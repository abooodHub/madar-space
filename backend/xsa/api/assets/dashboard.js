
    let currentSpaceId = null;
    let currentActiveSessions = [];
    let allAccountsData = [];
    let showAllSpaces = false;
    let currentSpacesSort = 'newest';
    let spacesSearchTimer = null;
    let spacesLoadInProgress = false;
    let currentTagFilter = null;
    let currentAccountFilter = 'all';
    let currentActionSpace = null;
    let editingAccountUsername = null;
    let lastApiError = '';
    let playerSpeakerSpaceId = '';
    let playerSpeakerSourceLabel = 'رصد واجهة X';
    let playerSpeakerTurns = [];
    let playerAudioContext = null;
    let playerAudioAnalyser = null;
    let playerAudioSource = null;
    let playerAudioFrame = 0;
    let playerAudioSmoothLevel = 0;

    async function fetchJson(url, options = {}) {
      try {
        const res = await fetch(url, options);
        if (!res.ok) {
          const raw = await res.text();
          try {
            const parsed = JSON.parse(raw);
            lastApiError = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail || parsed);
          } catch (_) { lastApiError = raw || `HTTP ${res.status}`; }
          throw new Error(lastApiError);
        }
        lastApiError = '';
        return await res.json();
      } catch (e) {
        if (!lastApiError) lastApiError = e.message || 'تعذر الاتصال بالخادم';
        console.error("API error:", e);
        return null;
      }
    }

    function readableApiError() {
      const message = lastApiError || 'حدث خطأ غير معروف';
      if (/already monitored/i.test(message)) return 'هذا الحساب مضاف للمراقبة مسبقاً.';
      if (/Invalid X username/i.test(message)) return 'اسم الحساب غير صحيح. أدخل اسم المستخدم فقط مثل: @username';
      if (/monitored host username is required/i.test(message)) return 'اختر الحساب المضيف للمساحة أولاً.';
      if (/not an active monitored account/i.test(message)) return 'الحساب المضيف غير موجود ضمن الحسابات النشطة المراقبة.';
      if (/Maximum concurrent recordings/i.test(message)) return 'وصلت إلى الحد الأقصى للتسجيلات المتزامنة. انتظر انتهاء تسجيل.';
      if (/already associated with a different host/i.test(message)) return 'هذه المساحة مرتبطة مسبقاً بحساب مضيف آخر.';
      if (/Maintenance mode/i.test(message)) return 'التسجيل متوقف مؤقتاً بسبب وضع الصيانة.';
      return message;
    }

    async function updateSystemHealth() {
      const data = await fetchJson('/health');
      if (!data) return;
      const badge = document.getElementById('maintenanceBadge');
      const items = document.getElementById('systemHealthItems');
      const joiningPanel = document.getElementById('joiningSessionsPanel');
      badge.classList.toggle('hidden', !data.maintenance_mode);
      const checks = [
        ['API', data.api_health ? data.api_health.status === 'healthy' : Boolean(data.database?.healthy)],
        ['المتصفح', data.browser_connected],
        ['المراقب', data.monitor_running],
        ['قاعدة البيانات', data.database && data.database.healthy],
        [`القرص ${data.disk ? data.disk.free_gb : '?'} GB`, data.disk && data.disk.healthy],
        [`FFmpeg ${data.recording_ffmpeg_count ?? '?'}`, Number(data.recording_ffmpeg_count) >= 0],
        [`قنوات ${data.slot_usage ? data.slot_usage.busy : '?'}/${data.slot_usage ? data.slot_usage.capacity : '?'}`, true]
      ];
      const jobsByType = data.processing_jobs_by_type || {};
      const postJobs = jobsByType.post_process || {};
      const postQueued = Number(postJobs.queued || 0);
      const postRunning = Number(postJobs.running || 0);
      const postFailed = Number(postJobs.failed || 0);
      checks.push([`ما بعد التسجيل: ${postRunning} تعمل · ${postQueued} تنتظر · ${postFailed} فشلت`, postFailed === 0]);
      const diarizeJobs = jobsByType.diarize || {};
      const diarizeWaiting = ['queued','retrying','waiting_for_resources','waiting_for_configuration'].reduce((n,state)=>n+Number(diarizeJobs[state]||0),0);
      const processingSummary = document.getElementById('processingCenterSummary');
      const processingActive = document.getElementById('processingActiveList');
      if (processingSummary) {
        processingSummary.innerHTML = [
          ['تفريغ وتحليل يعمل', postRunning, 'text-blue-200'],
          ['في الانتظار', postQueued, 'text-amber-200'],
          ['فشلت', postFailed, 'text-red-200'],
          ['فصل متحدثين منتظر', diarizeWaiting, 'text-purple-200']
        ].map(([label, value, color]) => `<div class="rounded-xl border border-xborder bg-black/20 p-3"><div class="text-[11px] text-gray-400">${label}</div><strong class="text-xl ${color}">${value}</strong></div>`).join('');
      }
      if (processingActive) {
        const active = data.active_post_processing || [];
        processingActive.innerHTML = active.length
          ? `مهام تعمل الآن: ${active.map(id => `<a class="text-sky-300 underline mr-2" href="/spaces/${encodeURIComponent(id)}/analysis">${escapeHtml(id)}</a>`).join('')}`
          : 'لا توجد مهام تفريغ صوتي نشطة الآن.';
        if (diarizeWaiting || Number(diarizeJobs.running || 0)) processingActive.innerHTML += `<div class="mt-2 text-amber-200">${escapeHtml(diarizationWorkerMessage(data.diarization_health))}</div>`;
      }
      if (diarizeWaiting > 0) {
        checks.push([`تمييز المتحدثين: ${diarizeWaiting} بانتظار عامل التحليل`, 'pending']);
      }
      const pill = ([label, ok]) => `
        <span class="px-2 py-1 rounded-md border ${ok === 'pending' ? 'border-amber-500/30 bg-amber-950/30 text-amber-200' : ok ? 'border-emerald-500/20 bg-emerald-950/30 text-emerald-200' : 'border-red-500/30 bg-red-950/40 text-red-200'}">
        <i class="fa-solid ${ok === 'pending' ? 'fa-clock' : ok ? 'fa-circle-check' : 'fa-circle-exclamation'} ml-1"></i>${escapeHtml(label)}
        </span>`;
      const serviceChecks = checks.slice(0, 7);
      const processingChecks = checks.slice(7);
      items.innerHTML = `
        <div class="health-status-group">
          <span class="health-group-label">الخدمات</span>
          <div class="health-status-pills">${serviceChecks.map(pill).join('')}</div>
        </div>
        <div class="health-status-group">
          <span class="health-group-label">المعالجة</span>
          <div class="health-status-pills">${processingChecks.length ? processingChecks.map(pill).join('') : '<span class="health-group-empty">لا توجد مهام نشطة</span>'}</div>
        </div>`;
      const joining = data.joining_sessions || [];
      joiningPanel.classList.toggle('hidden', joining.length === 0);
      joiningPanel.innerHTML = joining.map(s => {
        const checkpoints = {
          join_started: 'بدء الانضمام', opening_space: 'فتح المساحة', navigating_to_space: 'الانتقال للمساحة',
          space_page_loaded: 'تحميل صفحة المساحة', waiting_for_listen_controls: 'انتظار زر الاستماع',
          checking_anonymous_listening: 'التحقق من الاستماع المجهول', waiting_for_player: 'انتظار مشغل المساحة',
          verifying_host: 'التحقق من المضيف', host_verified: 'تم التحقق من المضيف',
          checking_identity_exposure: 'فحص إخفاء الهوية', loading_space_metadata: 'تحميل بيانات المساحة',
          starting_recorder: 'بدء التسجيل'
        };
        const age = Number(s.join_age_seconds || 0);
        const progress = Number(s.last_progress_seconds_ago || 0);
        const checkpoint = checkpoints[s.join_checkpoint] || s.join_checkpoint || 'بدء الانضمام';
        return `<div class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-500/20 bg-amber-950/20 px-3 py-2 text-[10px] text-amber-100">
          <span><b>JOINING</b> · ${escapeHtml(checkpoint)} · ${Math.floor(age)} / ${Number(s.join_deadline_seconds || 120)} sec · آخر تقدم: ${Math.floor(progress)} sec ago</span>
          <button onclick="cancelJoin('${escapeHtml(s.space_id)}')" class="rounded-md border border-red-500/30 bg-red-950/40 px-2 py-1 text-red-200">إلغاء الانضمام بأمان</button>
        </div>`;
      }).join('');
    }

    __XSA_PROCESSING_UI__
    async function loadProcessingJobs() {
      const container = document.getElementById('processingJobRows');
      if (!container) return;
      const response = await fetchJson('/processing/jobs?limit=40');
      if (!response) {
        container.innerHTML = '<div class="text-xs text-amber-200">تعذر تحميل تفاصيل المهام. أعد المحاولة بعد تحديث الخدمة.</div>';
        return;
      }
      const relevant = (response.items || []).filter(job =>
        ['running', 'queued', 'retrying', 'failed', 'waiting_for_resources', 'waiting_for_configuration'].includes(job.state)
      );
      const waitingDiarization = relevant.filter(isWaitingDiarizationJob);
      const visible = relevant.filter(job => !isWaitingDiarizationJob(job)).slice(0, 16);
      const activeMarkup = visible.map(job => {
        const labels = processingJobLabels(job);
        const windows = Number(job.total_windows) > 0
          ? ` · نوافذ Whisper ${Number(job.completed_windows || 0)}/${Number(job.total_windows)}` : '';
        const elapsed = job.started_at
          ? ` · منذ ${Math.max(0, Math.floor((Date.now() - Date.parse(job.started_at + (job.started_at.endsWith('Z') ? '' : 'Z'))) / 60000))} د` : '';
        const spaceLink = job.space_id
          ? `<a href="/spaces/${encodeURIComponent(job.space_id)}/analysis" class="text-sky-300 underline">${escapeHtml(job.space_id)}</a>`
          : '<span class="text-gray-500">—</span>';
        return `<div class="rounded-xl border border-xborder bg-black/20 p-3 text-xs flex flex-wrap justify-between gap-2">
          <div><b>${escapeHtml(labels.type)}</b> · ${spaceLink}<div class="text-gray-400 mt-1">${escapeHtml(labels.stage)}${windows}${elapsed}</div></div>
          <div class="text-left"><span class="${job.state === 'failed' ? 'text-red-300' : job.state === 'running' ? 'text-blue-200' : 'text-amber-200'}">${escapeHtml(labels.state)}</span><div class="text-gray-500">${escapeHtml(labels.attempt)}${job.worker_id ? ' · ' + escapeHtml(labels.worker) : ''}</div></div>
          ${job.error ? `<div class="w-full text-red-200/80">${escapeHtml(String(job.error).slice(0, 250))}</div>` : ''}
        </div>`;
      }).join('');
      const waitingMarkup = waitingDiarization.length
        ? `<details class="rounded-xl border border-xborder bg-black/20 p-3 text-xs"><summary class="cursor-pointer text-amber-200">${waitingDiarization.length} مهام فصل متحدثين محفوظة بانتظار العامل · عرض المعرفات</summary><div class="mt-3 flex flex-wrap gap-2">${waitingDiarization.map(job => `<a class="text-sky-300 underline" href="/spaces/${encodeURIComponent(job.space_id)}/analysis">${escapeHtml(job.space_id)}</a>`).join('')}</div></details>`
        : '';
      container.innerHTML = activeMarkup || waitingMarkup
        ? activeMarkup + waitingMarkup
        : '<div class="text-xs text-gray-400">لا توجد مهام نشطة أو فاشلة في القائمة الحديثة.</div>';
    }

    async function cancelJoin(spaceId) {
      if (!confirm(`إلغاء محاولة الانضمام للمساحة ${spaceId}؟ سيتم انتظار إغلاق الصفحة والتأكد من عدم وجود FFmpeg.`)) return;
      const result = await fetchJson(`/admin/spaces/${encodeURIComponent(spaceId)}/join`, {
        method: 'DELETE', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reason: 'Operator cancelled JOINING from dashboard'})
      });
      if (result) { updateSystemHealth(); updateLiveStatus(); loadSpaces(); }
      else alert(`تعذر إلغاء الانضمام: ${readableApiError()}`);
    }

    function syncDashboardKpis() {
      const pairs = [
        ['statTotalSpaces', 'heroSpacesKpi'],
        ['accountCount', 'heroAccountsKpi']
      ];
      for (const [sourceId, targetId] of pairs) {
        const source = document.getElementById(sourceId);
        const target = document.getElementById(targetId);
        if (source && target && source.textContent.trim()) target.textContent = source.textContent.trim();
      }
      const liveText = document.getElementById('liveFeedCountBadge')?.textContent || '';
      const liveCount = liveText.match(/\d+/)?.[0];
      const liveTarget = document.getElementById('heroLiveKpi');
      if (liveTarget && liveCount !== undefined) liveTarget.textContent = liveCount;
    }

    function observeDashboardKpis() {
      syncDashboardKpis();
      for (const id of ['statTotalSpaces', 'accountCount', 'liveFeedCountBadge']) {
        const source = document.getElementById(id);
        if (source) new MutationObserver(syncDashboardKpis).observe(source, { childList: true, characterData: true, subtree: true });
      }
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function urlComponent(value) {
      return encodeURIComponent(String(value ?? '')).replace(/[!'()*]/g,
        ch => '%' + ch.charCodeAt(0).toString(16));
    }

    function inlineCall(name, args) {
      const jsArgs = args.map(value => JSON.stringify(String(value ?? '')
        .replace(/</g, '\u003c').replace(/>/g, '\u003e').replace(/&/g, '\u0026')
        .replace(new RegExp(String.fromCharCode(0x2028), 'g'), '\\u2028')
        .replace(new RegExp(String.fromCharCode(0x2029), 'g'), '\\u2029'))).join(',');
      return escapeHtml(`${name}(${jsArgs})`);
    }

    function formatSeconds(secs) {
      if (!secs) return '00:00:00';
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = Math.floor(secs % 60);
      return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    // --- 0. Live on X Feed (بث مباشر على X) ---
    async function updateLiveFeed() {
      const data = await fetchJson('/live/feed');
      const container = document.getElementById('liveFeedGrid');
      const badge = document.getElementById('liveFeedCountBadge');
      if (!data || !data.feed) return;

      badge.innerText = `${data.feed.length} مساحة`;

      if (data.feed.length === 0) {
        container.innerHTML = `<div class="text-xs text-gray-400 py-6 col-span-full text-center">${data.discovery?.status==='rate_limited'?`اكتشاف X مؤجل بسبب حد الطلبات؛ إعادة التحقق بعد نحو ${Math.ceil(data.discovery.retry_after_seconds/60)} دقيقة. القائمة الفارغة لا تؤكد عدم وجود مساحات مباشرة.`:data.discovery?.status==='authentication_required'?'رفض X المصادقة على الفحص؛ افحص تسجيل الدخول عبر noVNC. لا يمكن تأكيد حالة الروابط الآن.':data.discovery?.status==='offline'?'خدمة الاكتشاف غير متاحة.':'لم تُكتشف مساحات مؤكدة في آخر فحص.'}</div>`;
        return;
      }

      container.innerHTML = data.feed.map(s => {
        const isRec = s.is_recording;
        const fallback = `https://ui-avatars.com/api/?name=${urlComponent(s.host_username || 'Space')}&background=7856ff&color=fff`;
        const avatar = s.host_avatar || fallback;
        const roleLabel = !s.host_verified ? 'المضيف قيد التحقق' : s.role === 'speaker' ? 'يتحدث' : (s.role === 'listener' ? 'يستمع' : 'يستضيف');
        const roleColor = s.role === 'speaker' ? 'bg-purple-900/50 text-purple-300 border-purple-500/30' :
                          (s.role === 'listener' ? 'bg-gray-800 text-gray-300 border-gray-700' : 'bg-blue-900/50 text-blue-300 border-blue-500/30');

        return `
          <div onclick="${inlineCall('openSpaceActionModal', [s.space_id])}"
               class="rounded-2xl bg-black/60 hover:bg-gray-900/90 border ${isRec ? 'border-red-500/60 ring-1 ring-red-500/40' : 'border-xborder'} p-3.5 transition shadow-lg cursor-pointer group flex flex-col justify-between min-h-[140px] relative overflow-hidden">

            ${isRec ? '<div class="absolute top-2 left-2 flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-red-950/80 border border-red-500/40 text-[9px] font-bold text-red-400 animate-pulse"><span class="w-1.5 h-1.5 rounded-full bg-red-500"></span>يسجل الآن</div>' : ''}

            <!-- Header: Host info & Role -->
            <div>
              <div class="flex items-center gap-2 mb-2">
                <img src="${escapeHtml(avatar)}" onerror="this.onerror=null;this.src='${escapeHtml(fallback)}'" class="w-8 h-8 rounded-full object-cover border border-xborder flex-shrink-0">
                <div class="min-w-0 flex-1">
                  <div class="font-bold text-xs text-white truncate flex items-center gap-1">
                    <span>${escapeHtml(s.host_display_name || s.host_username || 'المضيف قيد التحقق')}</span>
                    ${s.host_verified?'<i class="fa-solid fa-circle-check text-[10px] text-xblue"></i>':''}
                  </div>
                  <span class="text-[9px] px-1.5 py-0.2 rounded font-semibold border ${roleColor} inline-block">${roleLabel}</span>
                </div>
              </div>

              <!-- Title -->
              <h4 class="font-bold text-xs text-gray-200 group-hover:text-xblue transition line-clamp-2 leading-snug">
                ${escapeHtml(s.title || 'مساحة قيد التحقق')}
              </h4><p class="text-[10px] text-amber-300 mt-2">${s.verification_state==='pending'?'رابط مكتشف — لم يُتحقق من كونه مباشرًا بعد':s.verification_state==='stale'?'آخر تحقق قديم؛ استمرار البث غير مؤكد':s.verification_state==='joining'?'جارٍ الانضمام — لم يبدأ التسجيل':s.verification_state==='confirmed_live'?'بث مباشر متحقق منه':''}</p>
            </div>

            <!-- Footer: Listeners + Action icon -->
            <div class="flex items-center justify-between text-[11px] pt-2 border-t border-xborder/40 text-gray-400 mt-2">
              <span class="px-2 py-0.5 rounded-full bg-purple-950/50 border border-purple-500/30 text-purple-300 font-bold text-[10px]">
                <i class="fa-solid fa-headphones ml-1"></i>+${s.listeners_count || '1'}
              </span>
              <span class="text-[10px] text-gray-400 group-hover:text-white flex items-center gap-1">
                <span>خيارات</span>
                <i class="fa-solid fa-chevron-left text-[9px]"></i>
              </span>
            </div>
          </div>
        `;
      }).join('');
      if(data.discovery?.status==='rate_limited')container.insertAdjacentHTML('afterbegin',`<div class="col-span-full text-xs text-amber-300 p-3">التحقق الجديد مؤجل بسبب مهلة X: نحو ${Math.ceil(data.discovery.retry_after_seconds/60)} دقيقة. تظهر الروابط السابقة بحالة تحقق صريحة.</div>`);if(data.discovery?.status==='authentication_required')container.insertAdjacentHTML('afterbegin','<div class="col-span-full text-xs text-amber-300 p-3">رفض X المصادقة على التحقق. افحص تسجيل الدخول عبر noVNC؛ الروابط غير مؤكدة.</div>');
    }

    async function openSpaceActionModal(spaceId) {
      const data = await fetchJson('/live/feed');
      if (!data || !data.feed) return;
      const s = data.feed.find(x => x.space_id === spaceId);
      if (!s) return;

      currentActionSpace = s;
      document.getElementById('modalSpaceHost').innerText = `@${s.host_username} (${s.host_display_name || ''})`;
      document.getElementById('modalSpaceRole').innerText = s.role === 'speaker' ? 'متحدث في المساحة' : (s.role === 'listener' ? 'مستمع في المساحة' : 'مستضيف المساحة (Host)');
      document.getElementById('modalSpaceTitle').innerText = s.title || 'مساحة مباشرة';
      document.getElementById('modalSpaceListeners').innerHTML = `<i class="fa-solid fa-headphones ml-1 text-purple-400"></i> ${s.listeners_count || '1'} مستمع`;

      const badge = document.getElementById('modalSpaceStatusBadge');
      const recBtn = document.getElementById('modalBtnRecordNow');
      if (s.is_recording) {
        badge.className = 'px-2 py-0.5 rounded-full text-[10px] font-bold bg-red-950 text-red-400 border border-red-500/40 animate-pulse';
        badge.innerText = '🔴 قيد التسجيل بالفيديو حالياً';
        recBtn.disabled = true;
        recBtn.className = 'w-full py-2.5 rounded-xl bg-gray-800 text-gray-400 font-bold text-xs flex items-center justify-center gap-2 cursor-not-allowed';
        recBtn.innerHTML = '<i class="fa-solid fa-check"></i> <span>جاري تسجيل هذه المساحة بالفيديو</span>';
      } else {
        badge.className = 'px-2 py-0.5 rounded-full text-[10px] font-bold bg-green-950 text-green-400 border border-green-500/40';
        badge.innerText = 'مباشر — غير مسجلة بعد';
        recBtn.disabled = false;
        recBtn.className = 'w-full py-2.5 rounded-xl bg-red-600 hover:bg-red-500 text-white font-bold text-xs flex items-center justify-center gap-2 shadow-lg shadow-red-600/20 transition';
        recBtn.innerHTML = '<i class="fa-solid fa-circle-dot"></i> <span>بدء التسجيل الآن بالفيديو</span>';
      }

      if(!s.is_recording && (['rate_limited','authentication_required'].includes(data.discovery?.status) || s.verification_state!=='confirmed_live' || !s.host_verified)){badge.innerText='التحقق من البث مؤجل أو غير مكتمل';recBtn.disabled=true;recBtn.innerText='يجب تأكيد البث والمضيف وانتهاء مهلة X أولًا';}

      const spaceUrl = `https://x.com/i/spaces/${s.space_id}`;
      document.getElementById('modalSpaceUrlInput').value = spaceUrl;
      document.getElementById('modalBtnOpenX').href = spaceUrl;

      const fallback = `https://ui-avatars.com/api/?name=${encodeURIComponent(s.host_username || 'Space')}&background=7856ff&color=fff`;
      document.getElementById('modalSpaceAvatar').src = s.host_avatar || fallback;

      document.getElementById('spaceActionModal').classList.remove('hidden');
    }

    function closeSpaceActionModal() {
      document.getElementById('spaceActionModal').classList.add('hidden');
      currentActionSpace = null;
    }

    function copySpaceModalUrl() {
      const input = document.getElementById('modalSpaceUrlInput');
      input.select();
      navigator.clipboard.writeText(input.value);
      const btn = document.getElementById('btnCopySpaceUrl');
      btn.innerHTML = '<i class="fa-solid fa-check ml-1 text-green-400"></i> تم النسخ';
      setTimeout(() => { btn.innerHTML = '<i class="fa-solid fa-copy ml-1"></i> نسخ'; }, 2500);
    }

    async function modalRecordNow() {
      if (!currentActionSpace) return;
      const url = `https://x.com/i/spaces/${currentActionSpace.space_id}`;
      const res = await fetchJson('/spaces/record', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ url: url, host: currentActionSpace.host_username })
      });
      if (res && ['started', 'already_recording'].includes(res.status)) {
        alert(res.status === 'already_recording' ? 'هذه المساحة قيد التسجيل بالفعل.' : `بدأ تسجيل المساحة ${res.space_id}.`);
        closeSpaceActionModal();
        updateLiveStatus();
        updateLiveFeed();
        loadSpaces();
      } else alert(`تعذر بدء التسجيل: ${readableApiError()}`);
    }

    // --- 1. Multi-Space Live Poller (Up to 10 slots) ---
    async function updateLiveStatus() {
      const data = await fetchJson('/live/details');
      const badge = document.getElementById('liveBadge');
      const badgeText = document.getElementById('liveBadgeText');
      const section = document.getElementById('activeSessionsSection');
      const grid = document.getElementById('activeSessionsGrid');
      const countBadge = document.getElementById('activeSessionsCountBadge');

      if (data && data.active && data.active.length > 0) {
        currentActiveSessions = data.active;
        badge.classList.remove('hidden');
        badge.classList.add('flex');
        badgeText.innerText = data.active.length > 1 ? `تسجيل ${data.active.length} مساحات بالتوازي` : 'تسجيل جاري';

        section.classList.remove('hidden');
        countBadge.innerText = `${data.active.length} تسجيلات نشطة`;

        grid.innerHTML = data.active.map((sess, idx) => {
          const spk = sess.active_speaker;
          const spkName = spk && spk.display_name ? spk.display_name : (spk && spk.username ? `@${spk.username}` : '');
          const spkText = spkName ? `${escapeHtml(spkName)}` : 'استماع / صمت';
          const silence = sess.audio_silence || {};
          const endState = sess.end_detector?.space_state || 'UNKNOWN';
          const silenceSeconds = Number(silence.silence_duration_seconds || 0);
          const silenceNotice = silence.audio_silent && silence.silence_suspected
            ? `<div class="rounded-lg border border-amber-500/30 bg-amber-950/30 px-3 py-1.5 text-[10px] text-amber-100">
                 صمت مستمر ${formatSeconds(silenceSeconds)} · حالة المساحة: ${escapeHtml(endState)}.
               </div>`
            : '';
          const isJoining = sess.duration_s === 0 || sess.state === 'joining' || !sess.state || sess.state === 'detected';
          const recorderStatus = isJoining
            ? '<span class="text-amber-300 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-spinner fa-spin text-xs"></i><span>جارٍ بدء التسجيل الصوتي...</span></span>'
            : (sess.ffmpeg_healthy === false
              ? '<span class="text-red-300 font-semibold flex items-center gap-1.5"><i class="fa-solid fa-triangle-exclamation text-xs"></i><span>FFmpeg يحتاج فحصاً</span></span>'
              : '<span class="text-xgreen font-semibold flex items-center gap-1.5"><i class="fa-solid fa-circle-check text-xs"></i><span>FFmpeg يعمل</span></span>');

          const hasValidAdmins = Array.isArray(sess.admins) && sess.admins.length > 0;
          const admins = hasValidAdmins ? sess.admins : [{
            username: (sess.host && sess.host !== 'auto') ? sess.host : '',
            display_name: (sess.host && sess.host !== 'auto') ? sess.host : 'جارٍ استخراج بيانات المضيف...',
            role: 'host',
            role_label: 'المضيف',
            avatar_url: `https://ui-avatars.com/api/?name=${urlComponent((sess.host && sess.host !== 'auto') ? sess.host : 'Space')}&background=7856ff&color=fff&bold=true`,
            is_muted: false,
            is_speaking: false
          }];

          const speakers = Array.isArray(sess.speakers) ? sess.speakers : [];

          // Helper to render a person bubble (Avatar, Name, Role badge, Mute/Live icon)
          const renderPersonBubble = (p) => {
            const isSpeaking = Boolean(p.is_speaking || (spk && spk.username && p.username && spk.username.toLowerCase() === p.username.toLowerCase()));
            const isHost = p.role === 'host';
            const isCohost = p.role === 'cohost';
            const roleBadgeClass = isHost
              ? 'bg-amber-950/90 text-amber-300 border-amber-500/40'
              : (isCohost ? 'bg-purple-950/90 text-purple-300 border-purple-500/40' : 'bg-gray-800 text-gray-300 border-gray-700');
            const avatarBorderClass = isSpeaking
              ? 'border-sky-400 ring-2 ring-sky-400/70 shadow-lg shadow-sky-500/30 scale-105'
              : (isHost ? 'border-amber-400/90' : (isCohost ? 'border-purple-400/90' : 'border-gray-700'));
            const fallbackAvatar = `https://ui-avatars.com/api/?name=${urlComponent(p.display_name || p.username || 'User')}&background=172033&color=fff&bold=true`;
            const avatarSrc = p.avatar_url || fallbackAvatar;
            const unameDisplay = (p.username && p.username !== 'auto') ? `@${escapeHtml(p.username)}` : '';
            const profileUrl = (p.username && p.username !== 'auto') ? `https://x.com/${encodeURIComponent(p.username)}` : '#';

            return `
              <a href="${profileUrl}" ${p.username && p.username !== 'auto' ? 'target="_blank" rel="noopener noreferrer"' : ''}
                 title="${escapeHtml(p.display_name || p.username)} ${unameDisplay} — ${p.role_label || p.role}"
                 class="flex flex-col items-center gap-1 min-w-[70px] max-w-[85px] p-1.5 rounded-xl transition ${isSpeaking ? 'bg-sky-950/40' : 'hover:bg-white/5'} group flex-shrink-0 text-center">
                <div class="relative">
                  <img src="${escapeHtml(avatarSrc)}" onerror="this.onerror=null;this.src='${escapeHtml(fallbackAvatar)}'"
                       class="w-11 h-11 rounded-full object-cover border-2 transition-transform duration-200 ${avatarBorderClass}">
                  ${p.is_muted ? '<span class="absolute -bottom-1 -right-1 w-4 h-4 rounded-full bg-red-950 border border-red-500 flex items-center justify-center text-[8px] text-red-300" title="الميكروفون مكتوم"><i class="fa-solid fa-microphone-slash"></i></span>' : ''}
                  ${isSpeaking ? '<span class="absolute -top-1 -right-1 flex h-3.5 w-3.5"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-75"></span><span class="relative inline-flex rounded-full h-3.5 w-3.5 bg-sky-500 border border-black flex items-center justify-center text-[7px] text-white"><i class="fa-solid fa-volume-high"></i></span></span>' : ''}
                </div>
                <div class="w-full">
                  <span class="block truncate text-[10px] font-bold text-white group-hover:text-sky-300 transition leading-tight">
                    ${escapeHtml(p.display_name || p.username)}
                  </span>
                  ${p.username && p.username !== 'auto' ? `<span class="block text-[8px] font-mono text-gray-400 truncate text-center" dir="ltr"><bdi>@${escapeHtml(p.username)}</bdi></span>` : ''}
                  <span class="inline-block text-[8px] font-bold mt-0.5 px-1.5 py-0.2 rounded border ${roleBadgeClass}">
                    ${escapeHtml(p.role_label || p.role)}
                  </span>
                </div>
              </a>
            `;
          };

          const adminsMarkup = admins.map(renderPersonBubble).join('');
          const speakersMarkup = speakers.length > 0
            ? speakers.map(renderPersonBubble).join('')
            : '<div class="text-[11px] text-gray-500 py-2.5 px-2 flex items-center gap-1.5"><i class="fa-solid fa-microphone-slash text-[10px]"></i> لا يوجد متحدثون إضافيون حالياً</div>';

          return `
            <div class="rounded-2xl bg-gradient-to-br from-red-950/30 via-xcard to-gray-900 border border-red-500/30 p-4 shadow-xl relative overflow-hidden flex flex-col justify-between gap-3">
              <!-- Header: Channel badge, live timer, space title -->
              <div>
                <div class="flex items-center justify-between mb-2">
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] font-bold px-2 py-0.5 rounded-lg bg-red-900/60 text-red-300 border border-red-500/30 flex items-center gap-1.5 font-mono">
                      <span class="rec-dot" style="width:6px;height:6px;"></span>
                      <i class="fa-solid fa-tv text-[9px]"></i> قناة ${sess.slot_id !== undefined ? sess.slot_id + 1 : idx + 1} (${sess.display || ':99'})
                    </span>
                    <span class="text-[10px] font-mono text-gray-400">ID: ${sess.space_id}</span>
                  </div>
                  <span class="font-mono text-xs font-bold text-white bg-black/60 px-2 py-0.5 rounded border border-red-500/30">
                    ${formatSeconds(sess.duration_s)}
                  </span>
                </div>

                <h3 class="font-bold text-sm text-white truncate mb-3" title="${escapeHtml(sess.title || 'مساحة مباشرة')}">
                  ${escapeHtml(sess.title || 'مساحة مباشرة')}
                </h3>

                <!-- Top Row: المضيف والشركاء (Host & Co-hosts) -->
                <div class="mb-2.5 rounded-xl bg-black/40 border border-white/5 p-2.5">
                  <div class="flex items-center gap-1.5 text-[10px] font-bold text-amber-300 mb-1.5">
                    <i class="fa-solid fa-crown text-amber-400 text-xs"></i>
                    <span>المضيف والشركاء (Host & Co-hosts)</span>
                    <span class="text-[9px] px-1.5 rounded-full bg-amber-950 text-amber-300 border border-amber-500/30 font-mono">${admins.length}</span>
                  </div>
                  <div class="flex items-center gap-2 overflow-x-auto pb-1 custom-scroll">
                    ${adminsMarkup}
                  </div>
                </div>

                <!-- Bottom Row: المتحدثون بالوقت الفعلي (Live Speakers) -->
                <div class="mb-2 rounded-xl bg-black/40 border border-white/5 p-2.5">
                  <div class="flex items-center justify-between mb-1.5">
                    <div class="flex items-center gap-2">
                      <div class="flex items-center gap-1.5 text-[10px] font-bold text-sky-300">
                        <i class="fa-solid fa-microphone text-sky-400 text-xs"></i>
                        <span>المتحدثون (Live Speakers)</span>
                        <span class="text-[9px] px-1.5 rounded-full bg-sky-950 text-sky-300 border border-sky-500/30 font-mono">${speakers.length}</span>
                      </div>
                      ${sess.listener_count ? `<span class="text-[9px] text-gray-400 bg-white/5 border border-white/10 px-1.5 py-0.5 rounded-full font-mono"><i class="fa-solid fa-headphones text-[8px] ml-1"></i>${sess.listener_count} مستمع</span>` : ''}
                    </div>
                    <div class="flex items-center gap-1 text-[10px]">
                      <span class="text-gray-400">الآن:</span>
                      <span class="font-bold text-sky-300 truncate max-w-[140px] flex items-center gap-1">
                        <span class="waveform-bar w-1 bg-sky-400 rounded-full" style="height:10px;"></span>
                        ${spkText}
                      </span>
                    </div>
                  </div>
                  <div class="flex items-center gap-2 overflow-x-auto pb-1 custom-scroll min-h-[56px]">
                    ${speakersMarkup}
                  </div>
                </div>

                ${silenceNotice}
              </div>

              <!-- Footer: File size, health & actions -->
              <div class="pt-2 border-t border-red-500/20 space-y-2">
                <div class="flex items-center justify-between text-[11px] text-gray-400">
                  <span><i class="fa-solid fa-file-video ml-1"></i> ${sess.file_mb} MB</span>
                  ${recorderStatus}
                </div>
                <div class="flex items-center gap-2">
                  <a href="${escapeHtml(sess.viewer_url || '#')}" target="_blank" rel="noopener noreferrer"
                     class="flex-1 rounded-lg border border-sky-500/30 bg-sky-950/40 hover:bg-sky-900/60 px-3 py-1.5 text-center text-xs font-bold text-sky-200 transition">
                    <i class="fa-solid fa-eye ml-1"></i> شاشة المساحة
                  </a>
                  <button onclick="stopRecording('${sess.space_id}')"
                          class="rounded-lg border border-red-500/30 bg-red-950/40 hover:bg-red-900/60 px-3 py-1.5 text-center text-xs font-bold text-red-200 transition">
                    <i class="fa-solid fa-stop ml-1"></i> إيقاف بأمان
                  </button>
                </div>
              </div>
            </div>
          `;
        }).join('');
      } else {
        currentActiveSessions = [];
        badge.classList.add('hidden');
        badge.classList.remove('flex');
        section.classList.add('hidden');
      }

      highlightActiveAccounts();
    }

    function highlightActiveAccounts() {
      const activeHosts = currentActiveSessions.map(s => (s.host || '').toLowerCase());
      document.querySelectorAll('[data-account-username]').forEach(el => {
        const u = el.getAttribute('data-account-username').toLowerCase();
        const badge = el.querySelector('.active-live-badge');
        if (activeHosts.includes(u)) {
          el.classList.add('border-red-500', 'ring-2', 'ring-red-500/40');
          if (badge) badge.classList.remove('hidden');
        } else {
          el.classList.remove('border-red-500', 'ring-2', 'ring-red-500/40');
          if (badge) badge.classList.add('hidden');
        }
      });
    }

    // --- 2. Monitored Accounts Filtering, Sorting & Tags ---
    async function loadAccounts() {
      const data = await fetchJson('/accounts');
      if (!data) return;
      allAccountsData = data;
      // Populate the host combobox datalist from monitored accounts
      const hostDatalist = document.getElementById('hostDatalist');
      if (hostDatalist) {
        hostDatalist.innerHTML = data.filter(a => a.active).map(a =>
          `<option value="${escapeHtml(a.username)}">@${escapeHtml(a.username)}${a.display_name ? ' — ' + escapeHtml(a.display_name) : ''}</option>`
        ).join('');
      }
      document.getElementById('accountCount').innerText = data.length;
      renderAccountsPreview();
      renderRecordingAccounts();
      loadTags();
      applyAccountFilters();
    }

    function renderAccountsPreview() {
      const grid = document.getElementById('accountsPreviewGrid');
      const count = document.getElementById('accountsPreviewCount');
      const button = document.getElementById('accountsToggleButton');
      if (!grid || !count) return;
      const newest = [...allAccountsData].sort((a, b) => {
        const aTime = Date.parse(a.added_at || '') || 0;
        const bTime = Date.parse(b.added_at || '') || 0;
        return bTime - aTime || (b.id || 0) - (a.id || 0);
      });
      const preview = newest.slice(0, 6);
      count.textContent = `${preview.length} من ${allAccountsData.length}`;
      if (button) {
        const isOpen = !document.getElementById('accountsContent')?.classList.contains('hidden');
        button.setAttribute('aria-expanded', String(isOpen));
        button.innerHTML = isOpen
          ? '<i class="fa-solid fa-chevron-up"></i><span>إخفاء القائمة والفلاتر</span>'
          : `<i class="fa-solid fa-chevron-down"></i><span>عرض القائمة والفلاتر (${allAccountsData.length})</span>`;
      }
      if (!preview.length) {
        grid.innerHTML = '<div class="text-xs text-gray-500 col-span-full">لا توجد حسابات مراقبة</div>';
        return;
      }
      grid.innerHTML = preview.map(account => {
        const username = escapeHtml(account.username || '');
        const name = escapeHtml(account.display_name || account.username || 'حساب');
        const avatar = escapeHtml(account.avatar_url || `https://ui-avatars.com/api/?name=${urlComponent(account.display_name || account.username)}&background=172033&color=fff&bold=true`);
        const state = account.active ? 'نشط' : 'متوقف';
        const stateClass = account.active ? 'text-emerald-300' : 'text-gray-500';
        return `<a href="https://x.com/${encodeURIComponent(account.username || '')}" target="_blank" rel="noopener noreferrer" title="@${username} — ${state}" class="flex min-w-0 items-center gap-2 rounded-xl border border-xborder/70 bg-black/25 px-2 py-2 hover:border-xblue/60 transition">
          <img src="${avatar}" onerror="this.onerror=null;this.src='https://ui-avatars.com/api/?name=${urlComponent(account.username || 'X')}&background=172033&color=fff'" class="h-9 w-9 shrink-0 rounded-full object-cover border border-white/10" alt="">
          <span class="min-w-0"><strong class="block truncate text-[11px] text-white">${name}</strong><span class="block truncate text-[10px] text-gray-400 dir-ltr text-right">@${username}</span><span class="block text-[9px] ${stateClass}">${state}</span></span>
        </a>`;
      }).join('');
    }

    function renderRecordingAccounts() {
      const grid = document.getElementById('recordingAccountsGrid');
      const count = document.getElementById('recordingAccountsCount');
      const recording = allAccountsData.filter(account => account.is_recording);
      count.innerText = `${recording.length} ${recording.length === 1 ? 'حساب يسجل' : 'حسابات تسجل'}`;
      if (!recording.length) {
        grid.innerHTML = '<div class="text-xs text-gray-500">لا يوجد حساب مراقب يسجل الآن</div>';
        return;
      }
      grid.innerHTML = recording.map(account => {
        const name = escapeHtml(account.display_name || account.username);
        const username = escapeHtml(account.username);
        const avatar = escapeHtml(account.avatar_url || `https://ui-avatars.com/api/?name=${urlComponent(account.display_name || account.username)}&background=7f1d1d&color=fff&bold=true`);
        return `<a class="recording-account-card" href="https://x.com/${encodeURIComponent(account.username)}" target="_blank" rel="noopener noreferrer" title="فتح حساب @${username} في X">
          <img src="${avatar}" onerror="this.onerror=null;this.src='https://ui-avatars.com/api/?name=${urlComponent(account.username)}&background=7f1d1d&color=fff'" class="w-10 h-10 rounded-full object-cover border border-red-500/50" alt="">
          <span class="min-w-0"><strong class="block text-xs text-white truncate max-w-44">${name}</strong><span class="block text-[10px] text-gray-400 dir-ltr text-right">@${username}</span><span class="block text-[9px] text-red-300 mt-1"><i class="fa-solid fa-circle text-[7px] ml-1 animate-pulse"></i>يسجل الآن</span></span>
        </a>`;
      }).join('');
    }

    function toggleAccountsPanel() {
      const panel = document.getElementById('accountsContent');
      const button = document.getElementById('accountsToggleButton');
      const isOpening = panel.classList.contains('hidden');
      panel.classList.toggle('hidden', !isOpening);
      button.setAttribute('aria-expanded', String(isOpening));
      button.innerHTML = isOpening
        ? '<i class="fa-solid fa-chevron-up"></i><span>إخفاء بقية الحسابات</span>'
        : '<i class="fa-solid fa-chevron-down"></i><span>عرض بقية الحسابات والفلاتر</span>';
    }

    async function loadTags() {
      const tags = await fetchJson('/tags');
      const container = document.getElementById('tagsPillsContainer');
      const suggestions = document.getElementById('tagsSuggestions');
      if (!tags) return;

      container.innerHTML = tags.map(t => `
        <button onclick="${inlineCall('toggleTagFilter', [t.name])}"
                class="px-2 py-0.5 rounded-md text-[11px] font-semibold transition flex items-center gap-1 ${currentTagFilter === t.name ? 'bg-xpurple text-white shadow' : 'bg-gray-800 text-gray-300 hover:text-white'}">
          <span>${escapeHtml(t.name)}</span>
          <span class="text-[9px] opacity-75 font-mono">(${t.count})</span>
        </button>
      `).join('');

      suggestions.innerHTML = tags.map(t => `
        <button type="button" onclick="${inlineCall('addTagSuggestion', [t.name])}" class="px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 text-[10px]">
          + ${escapeHtml(t.name)}
        </button>
      `).join('');
    }

    function toggleTagFilter(tagName) {
      currentTagFilter = currentTagFilter === tagName ? null : tagName;
      loadTags();
      applyAccountFilters();
    }

    function setAccountFilter(flt) {
      currentAccountFilter = flt;
      ['All', 'Recording', 'Live', 'Active', 'Paused'].forEach(k => {
        const btn = document.getElementById(`fltBtn${k}`);
        if (btn) btn.className = 'px-2.5 py-1 rounded-lg text-xs font-bold bg-gray-800 text-gray-300 hover:text-white transition';
      });
      const activeBtnMap = { 'all': 'fltBtnAll', 'recording': 'fltBtnRecording', 'live': 'fltBtnLive', 'active': 'fltBtnActive', 'paused': 'fltBtnPaused' };
      const activeId = activeBtnMap[flt];
      if (activeId) {
        document.getElementById(activeId).className = 'px-2.5 py-1 rounded-lg text-xs font-bold bg-xblue text-white transition';
      }
      applyAccountFilters();
    }

    function applyAccountFilters() {
      const q = (document.getElementById('accountSearchInput').value || '').trim().toLowerCase();
      const sortVal = document.getElementById('accountSortSelect').value;
      const container = document.getElementById('accountsGrid');
      const filteredBadge = document.getElementById('filteredAccountCount');

      let list = [...allAccountsData];

      // 1. Filter by search query
      if (q) {
        list = list.filter(a => (a.username || '').toLowerCase().includes(q) || (a.display_name || '').toLowerCase().includes(q));
      }

      // 2. Filter by status chip
      if (currentAccountFilter === 'recording') {
        list = list.filter(a => a.is_recording);
      } else if (currentAccountFilter === 'live') {
        list = list.filter(a => a.is_live);
      } else if (currentAccountFilter === 'active') {
        list = list.filter(a => a.active);
      } else if (currentAccountFilter === 'paused') {
        list = list.filter(a => !a.active);
      }

      // 3. Filter by Tag
      if (currentTagFilter) {
        list = list.filter(a => (a.tags || []).includes(currentTagFilter));
      }

      // 4. Sort
      if (sortVal === 'name') {
        list.sort((a, b) => (a.display_name || a.username).localeCompare(b.display_name || b.username, 'ar'));
      } else if (sortVal === 'spaces_desc') {
        list.sort((a, b) => (b.spaces_count || 0) - (a.spaces_count || 0));
      } else if (sortVal === 'last_space') {
        list.sort((a, b) => new Date(b.last_space_at || 0) - new Date(a.last_space_at || 0));
      } else if (sortVal === 'recent') {
        list.sort((a, b) => new Date(b.added_at || 0) - new Date(a.added_at || 0));
      }
      if (currentAccountFilter === 'all' && !q && !currentTagFilter) {
        list = list.filter(account => !account.is_recording);
      }

      // Render count badge
      if (list.length !== allAccountsData.length) {
        filteredBadge.innerText = `${list.length} معروض`;
        filteredBadge.classList.remove('hidden');
      } else {
        filteredBadge.classList.add('hidden');
      }

      if (list.length === 0) {
        container.innerHTML = '<div class="text-xs text-gray-500 py-6 col-span-full text-center">لا توجد حسابات مطابقة للفلتر</div>';
        return;
      }

      container.innerHTML = list.map(a => {
        const displayLabel = urlComponent(a.display_name || a.username);
        const fallback = `https://ui-avatars.com/api/?name=${displayLabel}&background=1d9bf0&color=fff&bold=true&size=128`;
        const avatar = a.avatar_url || fallback;
        const tagsHtml = (a.tags || []).slice(0, 2).map(t => `<span class="px-1 py-0.2 rounded bg-purple-900/40 text-[9px] text-purple-300 truncate max-w-[50px] inline-block font-semibold">#${escapeHtml(t)}</span>`).join(' ');

        return `
          <div data-account-username="${escapeHtml(a.username)}"
               class="flex flex-col items-center justify-between p-2 rounded-2xl bg-black/40 hover:bg-gray-900/70 border border-xborder transition relative group text-center min-h-[145px]">

            <!-- Tag button top corner -->
            <button onclick="${inlineCall('openTagsModal', [a.username])}" title="إدارة العلامات (Tags)"
                    class="absolute top-1.5 left-1.5 text-gray-500 hover:text-xpurple text-[10px] p-1 rounded-md opacity-0 group-hover:opacity-100 transition">
              <i class="fa-solid fa-tag"></i>
            </button>

            <!-- Avatar with Active / Live Status -->
            <div class="relative mt-1">
            <img src="${escapeHtml(avatar)}" onerror="this.onerror=null;this.src='${escapeHtml(fallback)}'"
                   alt="${escapeHtml(a.username)}" class="w-11 h-11 rounded-full object-cover border-2 ${a.is_recording ? 'border-red-500 ring-2 ring-red-500/50' : (a.active ? 'border-xgreen' : 'border-gray-600')}">
              <span class="w-2.5 h-2.5 rounded-full ${a.is_recording ? 'bg-red-500 animate-ping' : (a.active ? 'bg-xgreen' : 'bg-gray-600')} absolute bottom-0 right-0 border-2 border-xcard"></span>
            </div>

            <!-- Name and Handle -->
            <div class="w-full min-w-0 px-1 my-1">
              <div class="font-bold text-xs text-white truncate leading-tight" title="${escapeHtml(a.display_name || a.username)}">${escapeHtml(a.display_name || a.username)}</div>
              <div class="text-[10px] text-gray-400 truncate dir-ltr text-center" title="@${escapeHtml(a.username)}">@${escapeHtml(a.username)}</div>
              <div class="text-[9px] ${a.last_checked_at ? 'text-gray-500' : 'text-amber-300'} mt-0.5" title="${a.last_checked_at ? `آخر فحص: ${escapeHtml(new Date(a.last_checked_at).toLocaleString())}` : 'أضيف للحسابات ولم يصل إليه دور الفحص بعد'}">${a.last_checked_at ? `آخر فحص ${escapeHtml(new Date(a.last_checked_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}))}` : 'بانتظار أول فحص'}</div>

              <!-- Tags badges -->
              <div class="flex items-center justify-center gap-0.5 mt-1 min-h-[14px]">
                ${tagsHtml || '<span class="text-[8px] text-gray-600">بدون وسم</span>'}
              </div>

              <span class="text-[9px] ${a.is_recording ? 'text-red-400' : 'text-emerald-300'} font-bold block mt-0.5 ${a.is_live ? '' : 'hidden'}">
                <i class="fa-solid ${a.is_recording ? 'fa-circle text-red-500 animate-pulse' : 'fa-tower-broadcast text-emerald-400'} text-[8px] ml-0.5"></i> ${a.is_recording ? 'يسجل الآن' : 'مباشر — التسجيل غير نشط'}
              </span>
            </div>

            <!-- Action Buttons -->
            <div class="flex items-center gap-1 pt-1 border-t border-xborder/40 w-full justify-center">
              <button onclick="${inlineCall('toggleAccount', [a.username])}" title="${a.active ? 'إيقاف مؤقت' : 'تفعيل'}"
                      class="w-5 h-5 rounded-lg bg-gray-800/90 hover:bg-gray-700 flex items-center justify-center text-[9px] ${a.active ? 'text-xblue' : 'text-gray-400'} hover:text-white transition">
                <i class="fa-solid ${a.active ? 'fa-pause' : 'fa-play'}"></i>
              </button>
              <button onclick="${inlineCall('openTagsModal', [a.username])}" title="تعديل الوسوم"
                      class="w-5 h-5 rounded-lg bg-gray-800/90 hover:bg-gray-700 flex items-center justify-center text-[9px] text-gray-400 hover:text-xpurple transition">
                <i class="fa-solid fa-tag"></i>
              </button>
              <button onclick="${inlineCall('deleteAccount', [a.username])}" title="أرشفة الحساب مع حفظ سجله"
                      class="w-5 h-5 rounded-lg bg-gray-800/90 hover:bg-gray-700 flex items-center justify-center text-[9px] text-gray-400 hover:text-red-400 transition">
                <i class="fa-solid fa-box-archive"></i>
              </button>
            </div>
          </div>
        `;
      }).join('');

      highlightActiveAccounts();
    }

    async function addAccount() {
      const input = document.getElementById('newAccountInput');
      const username = input.value.trim().replace(/^@+/, '');
      if (!username) { alert('أدخل اسم حساب X أولاً.'); input.focus(); return; }
      const button = input.parentElement.querySelector('button[onclick="addAccount()"]');
      if (button) button.disabled = true;
      const result = await fetchJson('/accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username})
      });
      if (button) button.disabled = false;
      if (!result) { alert(`تعذرت إضافة @${username}: ${readableApiError()}`); return; }
      input.value = '';
      await loadAccounts();
      alert(`تمت إضافة @${username} إلى قائمة المراقبة. سيظهر نشاطه عند اكتشاف مساحة عامة ومتاح سماعها.`);
    }

    async function refreshAvatars() {
      const btn = document.querySelector('button[onclick="refreshAvatars()"]');
      if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-rotate fa-spin text-[10px]"></i> <span>جارٍ التحديث...</span>'; }
      await fetchJson('/accounts/refresh-avatars', {method: 'POST'});
      setTimeout(() => {
        loadAccounts();
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-rotate text-[10px]"></i> <span>تحديث الصور</span>'; }
      }, 5000);
    }

    async function toggleAccount(u) {
      await fetchJson(`/accounts/${u}/toggle`, {method: 'PUT'});
      loadAccounts();
    }

    async function deleteAccount(u) {
      if (!confirm(`أرشفة الحساب @${u} وإيقاف استطلاعه مع إبقاء سجله ومساحاته؟`)) return;
      await fetchJson(`/accounts/${u}`, {method: 'DELETE'});
      loadAccounts();
    }

    // --- Tag Editing Modal ---
    function openTagsModal(username) {
      editingAccountUsername = username;
      const acc = allAccountsData.find(a => a.username === username);
      document.getElementById('tagsModalUsername').innerText = `@${username}`;
      document.getElementById('tagsModalInput').value = (acc && acc.tags) ? acc.tags.join(', ') : '';
      document.getElementById('tagsModal').classList.remove('hidden');
    }

    function closeTagsModal() {
      document.getElementById('tagsModal').classList.add('hidden');
      editingAccountUsername = null;
    }

    function addTagSuggestion(tagName) {
      const input = document.getElementById('tagsModalInput');
      const current = input.value.split(',').map(s => s.trim()).filter(Boolean);
      if (!current.includes(tagName)) {
        current.push(tagName);
        input.value = current.join(', ');
      }
    }

    async function saveAccountTags() {
      if (!editingAccountUsername) return;
      const raw = document.getElementById('tagsModalInput').value;
      const tags = raw.split(',').map(s => s.trim()).filter(Boolean);
      await fetchJson(`/accounts/${editingAccountUsername}/tags`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ tags })
      });
      closeTagsModal();
      loadAccounts();
    }

    // --- 3. Spaces Library Cards مع معاينة الفيديو والصور المصغرة ---
    function updateLibraryCards(container, markup) {
      if (!container || container.innerHTML === markup) return;
      const scrollTop = window.scrollY;
      container.innerHTML = markup;
      // Replacing a large list can trigger browser scroll anchoring. Keep the
      // user's viewport at the same document position after the cards settle.
      requestAnimationFrame(() => {
        if (Math.abs(window.scrollY - scrollTop) < 2) return;
        document.documentElement.style.scrollBehavior = 'auto';
        window.scrollTo(0, scrollTop);
        requestAnimationFrame(() => { document.documentElement.style.scrollBehavior = ''; });
      });
    }

    async function loadSpaces() {
      if (spacesLoadInProgress) return;
      spacesLoadInProgress = true;
      const container = document.getElementById('spacesCardsContainer');
      const statTotal = document.getElementById('statTotalSpaces');
      try {
      const includeFailed = document.getElementById('libraryScopeSelect')?.value === 'all';
      const data = await fetchJson(`/spaces?include_failed=${includeFailed}`);
      if (!data) {
        const countLabel = document.getElementById('libraryCountLabel');
        if (countLabel) countLabel.innerText = 'تعذر تحميل التسجيلات';
        if (container?.querySelector('[data-library-loader]')) {
          updateLibraryCards(container, '<div class="text-center py-10 text-xs text-amber-200 bg-xcard/40 rounded-2xl border border-amber-500/20">تعذر تحميل مكتبة المساحات. <button onclick="loadSpaces()" class="mr-2 underline">أعد المحاولة</button></div>');
        }
        return;
      }

      statTotal.innerText = data.length;
      const countLabel = document.getElementById('libraryCountLabel');
      const query = (document.getElementById('librarySearchInput')?.value || '').trim().toLocaleLowerCase();
      const filteredSpaces = data.filter(space => [space.title, space.host_username, space.host_display_name, space.space_id, space.folder_path]
        .some(value => String(value || '').toLocaleLowerCase().includes(query)));
      if (countLabel) {
        const collectionLabel = includeFailed ? 'جلسة ومحاولة' : 'تسجيلاً محفوظاً';
        countLabel.innerText = query
          ? `${filteredSpaces.length} نتيجة من ${data.length} ${collectionLabel}`
          : `${data.length} ${collectionLabel}`;
      }
      const expandButton = document.getElementById('libraryExpandButton');
      if (expandButton) {
        expandButton.classList.toggle('hidden', data.length <= 5 || Boolean(query));
        expandButton.innerHTML = showAllSpaces
          ? '<i class="fa-solid fa-compress"></i><span>عرض أحدث 5 فقط</span>'
          : `<i class="fa-solid fa-list"></i><span>عرض كل التسجيلات (${data.length})</span>`;
      }
      if (data.length === 0) {
        updateLibraryCards(container, '<div class="text-center py-12 text-xs text-gray-500 bg-xcard/40 rounded-2xl border border-xborder">لا توجد مساحات مسجلة حتى الآن</div>');
        return;
      }

      if (query && filteredSpaces.length === 0) {
        updateLibraryCards(container, '<div class="text-center py-10 text-xs text-gray-500 bg-xcard/40 rounded-2xl border border-xborder">ما لقينا تسجيل يطابق البحث. جرّب عنوان المساحة أو اسم المضيف أو جزءاً من المعرّف.</div>');
        return;
      }

      currentSpacesSort = document.getElementById('librarySortSelect')?.value || currentSpacesSort;
      const sortedSpaces = [...filteredSpaces].sort((a, b) => {
        if (currentSpacesSort === 'largest') return (Number(b.file_mb) || 0) - (Number(a.file_mb) || 0);
        const aTime = Date.parse(a.recording_started_at || a.detected_at || '') || 0;
        const bTime = Date.parse(b.recording_started_at || b.detected_at || '') || 0;
        return currentSpacesSort === 'oldest' ? aTime - bTime : bTime - aTime;
      });
      const visibleSpaces = query || showAllSpaces ? sortedSpaces : sortedSpaces.slice(0, 5);
      const markup = visibleSpaces.map(s => {
        const isRec = s.state === 'recording';
        const transcriptStatus = s.transcript_status || (s.has_transcript ? 'DONE' : 'NOT_TRANSCRIBED');
        const hasTrans = transcriptStatus === 'DONE';
        const hasSumm = s.status?.analysis === 'summary_ready';
        const hostAvatar = s.host_avatar || `https://unavatar.io/twitter/${urlComponent(s.host_username)}`;
        const fallback = `https://ui-avatars.com/api/?name=${urlComponent(s.host_display_name || s.host_username || 'Space')}&background=1d9bf0&color=fff`;
        const thumbUrl = `/spaces/${s.space_id}/thumbnail`;
        const hasVideo = Boolean(s.has_video);
        const mediaPlaceholder = isRec ? 'ملف الفيديو قيد الإنشاء' : 'لا يوجد ملف تسجيل';
        const processingLabels = {
          validate_media: 'فحص ملف التسجيل', build_playback: 'تجهيز نسخة التشغيل',
          thumbnail: 'إنشاء الصورة المصغرة', transcription: 'تحويل الصوت إلى نص',
          diarization_queue: 'تجهيز تحليل المتحدثين', semantic_analysis: 'تحليل محتوى المساحة',
          finalize_report: 'حفظ التقرير'
        };
        const processingStage = processingLabels[s.processing_stage] || '';
        const processingPercent = Math.max(0, Math.min(100, Math.round(Number(s.processing_progress) * 100 || 0)));
        const processingRunning = s.processing_state === 'running';
        const processingQueued = ['queued', 'retrying', 'waiting_for_resources', 'waiting_for_configuration'].includes(s.processing_state);
        const processingFailed = s.processing_state === 'failed';
        const stateLabels = {
          analyzed: 'اكتمل التحليل', processing: 'قيد المعالجة', error: 'تعذر الإكمال',
          rejected: 'مرفوض', recording: 'يسجل الآن', ended: 'انتهى التسجيل'
        };
        const qualityLabels = {
          NO_SPEECH: 'لا يوجد كلام في التفريغ — راجع الصوت',
          SILENT_CAPTURE_STOPPED: 'أوقف الالتقاط بعد صمت مستمر',
          AUDIO_MONITORING_UNAVAILABLE: 'قياس الصوت غير متاح — أوقف الالتقاط',
          PARTIAL: 'تسجيل جزئي', SPEECH_DETECTED: 'تم رصد كلام'
        };
        const qualityMarkup = qualityLabels[s.recording_quality]
          ? `<span class="text-[10px] px-2 py-0.5 rounded-full ${s.recording_quality === 'SPEECH_DETECTED' ? 'bg-emerald-900/30 text-emerald-200 border border-emerald-700/40' : 'bg-amber-900/30 text-amber-200 border border-amber-700/40'}">${escapeHtml(qualityLabels[s.recording_quality])}</span>`
          : '';
        const stateLabel = s.status?.capture === 'partial' ? 'تسجيل جزئي'
          : s.status?.capture === 'failed' ? 'فشل الالتقاط'
          : s.status?.analysis === 'summary_ready' ? 'ملخص جاهز'
          : s.status?.transcription === 'no_speech' ? 'لا كلام في التفريغ'
          : stateLabels[s.state] || s.state;
        const processingMarkup = processingRunning || processingQueued || processingFailed
          ? `<div class="space-y-1.5 max-w-md">
               <div class="flex items-center gap-2 text-[11px] ${processingFailed ? 'text-red-300' : 'text-blue-200'}">
                 <span>${processingFailed ? 'فشلت المعالجة' : processingQueued ? 'بانتظار المعالجة' : escapeHtml(processingStage || 'جاري المعالجة')}</span>
                 ${processingRunning ? `<span class="font-mono text-gray-400">${processingPercent}%</span>` : ''}
               </div>
               ${processingRunning ? `<div class="h-1.5 rounded-full bg-gray-800 overflow-hidden"><div class="h-full bg-xblue transition-all" style="width:${processingPercent}%"></div></div>` : ''}
               ${processingFailed && s.processing_error ? `<p class="text-[10px] text-red-200/80 break-words" title="${escapeHtml(s.processing_error)}">${escapeHtml(String(s.processing_error).slice(0, 220))}</p>` : ''}
             </div>`
          : '';
        const joinDiagnostic = !s.has_saved_artifact && (s.join_outcome || s.join_checkpoint)
          ? `<span class="text-[10px] text-amber-200">${escapeHtml(String(s.join_outcome || s.join_checkpoint).replaceAll('_', ' '))}</span>`
          : '';

        return `
          <div class="rounded-2xl bg-xcard hover:bg-xcardhover border border-xborder p-4 transition shadow-md flex flex-col md:flex-row items-start md:items-center justify-between gap-4">

            <!-- Thumbnail preview with duration badge -->
            <div ${hasVideo ? `onclick="${inlineCall('openVideoModal', [s.space_id, urlComponent(s.title || ''), s.host_username])}"` : ''}
                 class="relative w-full md:w-52 h-32 md:h-28 rounded-xl overflow-hidden bg-black/60 border border-xborder flex-shrink-0 ${hasVideo ? 'cursor-pointer group/thumb' : ''}">
              ${hasVideo ? `
                <img src="${thumbUrl}" onerror="this.style.display='none'" class="w-full h-full object-cover group-hover/thumb:scale-105 transition duration-300">
                <div class="absolute inset-0 bg-black/40 group-hover/thumb:bg-black/20 flex items-center justify-center transition">
                  <div class="w-10 h-10 rounded-full bg-xblue/90 text-white flex items-center justify-center shadow-lg group-hover/thumb:scale-110 transition">
                    <i class="fa-solid fa-play ml-0.5 text-xs"></i>
                  </div>
                </div>
              ` : `<div class="h-full flex flex-col items-center justify-center gap-2 text-gray-500 text-xs"><i class="fa-solid ${isRec ? 'fa-spinner fa-spin' : 'fa-box-open'} text-lg"></i><span>${mediaPlaceholder}</span></div>`}

              ${s.duration_seconds > 0 ? `
                <span class="absolute bottom-1.5 left-1.5 px-2 py-0.5 rounded bg-black/80 text-[10px] font-mono font-bold text-white">
                  ${formatSeconds(s.duration_seconds)}
                </span>
              ` : ''}
            </div>

            <!-- Space Info -->
            <div class="space-y-2 flex-1 min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-[10px] px-2.5 py-0.5 rounded-full font-bold uppercase ${
                  isRec ? 'bg-red-500/20 text-red-400 border border-red-500/30 animate-pulse' :
                  processingFailed || s.state === 'error' ? 'bg-red-500/15 text-red-300 border border-red-500/30' :
                  processingRunning || processingQueued ? 'bg-blue-500/15 text-blue-200 border border-blue-500/30' :
                  hasSumm ? 'bg-xpurple/20 text-xpurple border border-xpurple/30' :
                  hasTrans ? 'bg-xgreen/20 text-xgreen border border-xgreen/30' : 'bg-gray-800 text-gray-300'
                }">
                  ${isRec ? 'قيد التسجيل الآن' : processingRunning ? 'قيد المعالجة' : processingQueued ? 'بانتظار المعالجة' : processingFailed ? 'تعذرت المعالجة' : escapeHtml(stateLabel)}
                </span>
                ${hasTrans ? '<span class="text-[10px] px-2 py-0.5 rounded-full bg-blue-900/30 text-xblue border border-xblue/30"><i class="fa-solid fa-check ml-1"></i>نص مفرّغ</span>' : ''}
                ${hasSumm ? '<span class="text-[10px] px-2 py-0.5 rounded-full bg-purple-900/30 text-xpurple border border-xpurple/30"><i class="fa-solid fa-sparkles ml-1"></i>ملخص ذكي</span>' : ''}
                ${qualityMarkup}
                ${joinDiagnostic}
                <span class="text-[11px] font-mono text-gray-400">ID: ${s.space_id}</span>
              </div>

              <h3 onclick="${inlineCall('openInspector', [s.space_id])}" class="text-base font-bold text-white hover:text-xblue cursor-pointer transition leading-snug">
                ${escapeHtml(s.title)}
              </h3>

              <div class="flex flex-wrap items-center gap-4 text-xs text-gray-400">
                <div class="flex items-center gap-2">
                  <img src="${escapeHtml(hostAvatar)}" onerror="this.onerror=null;this.src='${escapeHtml(fallback)}'" class="w-5 h-5 rounded-full object-cover">
                  <span>المضيف: <b class="text-gray-300">${escapeHtml(s.host_display_name || (s.host_username !== 'unknown' ? '@' + s.host_username : 'غير مسجل'))}</b></span>
                </div>
                <span><i class="fa-regular fa-clock ml-1"></i> ${new Date(s.recording_started_at || s.detected_at).toLocaleString('ar-SA', { timeZone: 'Asia/Riyadh' })}</span>
                <span title="المصدر ${Number(s.file_mb || 0).toFixed(1)} MB · نسخة التشغيل ${Number(s.playback_mb || 0).toFixed(1)} MB"><i class="fa-solid fa-hard-drive ml-1"></i> ${Number(s.total_media_mb ?? s.file_mb ?? 0).toFixed(1)} MB إجمالي الوسائط</span>
                <span title="مجلد التسجيل"><i class="fa-solid fa-folder-open ml-1"></i>${escapeHtml(s.folder_path || s.folder_name || '')}</span>
              </div>
              ${processingMarkup}
            </div>

            <!-- Actions -->
            <div class="flex flex-wrap md:flex-col gap-2 flex-shrink-0 w-full md:w-auto">
              <a href="/spaces/${encodeURIComponent(s.space_id)}/analysis" target="_blank" rel="noopener noreferrer"
                 class="flex-1 md:flex-none px-4 py-2 rounded-xl bg-emerald-900/35 hover:bg-emerald-800/45 text-emerald-100 text-xs font-bold flex items-center justify-center gap-1.5 border border-emerald-700/40 transition">
                <i class="fa-solid fa-chart-line"></i><span>تحليل المساحة</span>
              </a>
              ${hasVideo ? `
                <button onclick="${inlineCall('openVideoModal', [s.space_id, urlComponent(s.title || ''), s.host_username])}"
                        class="flex-1 md:flex-none px-4 py-2 rounded-xl bg-xblue hover:bg-blue-600 text-white text-xs font-bold flex items-center justify-center gap-1.5 shadow transition">
                  <i class="fa-solid fa-circle-play"></i><span>معاينة الفيديو</span>
                </button>
              ` : ''}
              <a href="/spaces/${encodeURIComponent(s.space_id)}/info" target="_blank" rel="noopener noreferrer"
                 class="flex-1 md:flex-none px-4 py-2 rounded-xl bg-gray-800 hover:bg-gray-700 text-gray-100 text-xs font-bold flex items-center justify-center gap-1.5 border border-xborder transition">
                <i class="fa-regular fa-file-lines text-xblue"></i>
                <span>بطاقة المساحة</span>
              </a>
              ${(s.state === 'error' || processingFailed) && hasVideo ? `
                <button onclick="${inlineCall('retrySpaceProcessing', [s.space_id])}"
                        class="px-3 py-2 rounded-xl bg-amber-900/40 hover:bg-amber-800/60 text-amber-200 text-xs font-bold border border-amber-700/50 transition">
                  <i class="fa-solid fa-rotate-right ml-1"></i>إعادة معالجة التسجيل
                </button>
              ` : ''}
              ${s.state === 'error' ? `
                <button onclick="${inlineCall('quarantineSpace', [s.space_id])}"
                        class="px-3 py-2 rounded-xl bg-red-950/40 hover:bg-red-900/60 text-red-200 text-xs font-bold border border-red-700/50 transition">
                  <i class="fa-solid fa-box-archive ml-1"></i>عزل المساحة
                </button>
              ` : ''}
              ${s.state === 'processing' ? `
                <button onclick="${inlineCall('cancelProcessing', [s.space_id])}"
                        class="px-3 py-2 rounded-xl bg-red-950/40 hover:bg-red-900/60 text-red-200 text-xs font-bold border border-red-700/50 transition">
                  <i class="fa-solid fa-ban ml-1"></i>إلغاء المعالجة
                </button>
              ` : ''}

              ${hasVideo ? `<div class="flex gap-1.5">
                <button onclick="${inlineCall('openInspector', [s.space_id])}" title="فحص وتحليل المتحدثين"
                        class="flex-1 md:flex-none px-3 py-1.5 rounded-xl bg-gray-800/90 hover:bg-gray-700 text-xs text-white border border-xborder flex items-center justify-center gap-1 transition">
                  <i class="fa-solid fa-chart-simple"></i>
                  <span>التحليل</span>
                </button>
                <button onclick="${inlineCall('quickTranscribe', [s.space_id])}" title="تفريغ صوتي"
                        class="px-2.5 py-1.5 rounded-xl bg-gray-800/80 hover:bg-gray-700 text-xs text-gray-300 border border-xborder transition">
                  <i class="fa-solid fa-file-lines"></i>
                </button>
                <button onclick="${inlineCall('quickAnalyze', [s.space_id])}" title="ملخص بالذكاء الاصطناعي"
                        class="px-2.5 py-1.5 rounded-xl bg-gray-800/80 hover:bg-gray-700 text-xs text-gray-300 border border-xborder transition">
                  <i class="fa-solid fa-wand-magic-sparkles"></i>
                </button>
                <button onclick="${inlineCall('window.open', [`/spaces/${s.space_id}/transcript/srt`, '_blank'])}" title="تحميل SRT"
                        class="px-2.5 py-1.5 rounded-xl bg-gray-800/80 hover:bg-gray-700 text-xs text-gray-300 border border-xborder transition">
                  <i class="fa-solid fa-download"></i>
                </button>
              </div>` : ''}
            </div>

          </div>
        `;
      }).join('');
      updateLibraryCards(container, markup);
      } finally {
        spacesLoadInProgress = false;
      }
    }

    function toggleAllSpaces() {
      showAllSpaces = !showAllSpaces;
      loadSpaces();
    }

    function searchSpaces() {
      clearTimeout(spacesSearchTimer);
      spacesSearchTimer = setTimeout(loadSpaces, 220);
    }

    // --- Video Player Modal ---
    function resetPlayerSpeakerDisplay(message = 'لا توجد مداخلة مرصودة عند هذا التوقيت') {
      const name = document.getElementById('playerSpeakerName');
      const meta = document.getElementById('playerSpeakerMeta');
      const strip = document.getElementById('playerSpeakerStrip');
      if (name) name.textContent = message;
      if (meta) meta.textContent = 'توقيت المتحدث غير متاح';
      if (strip) strip.classList.remove('is-speaking');
    }

    async function loadPlayerSpeakerTimeline(spaceId) {
      playerSpeakerSpaceId = spaceId;
      playerSpeakerTurns = [];
      playerSpeakerSourceLabel = 'رصد واجهة X';
      renderPlayerSpeakerTrack();
      resetPlayerSpeakerDisplay('جارٍ تحميل توقيت المتحدثين…');
      const data = await fetchJson(`/spaces/${encodeURIComponent(spaceId)}/speaker-timeline`);
      if (playerSpeakerSpaceId !== spaceId) return;
      if (data && Array.isArray(data.turns) && data.turns.length) {
        playerSpeakerTurns = data.turns
          .filter(turn => Number.isFinite(Number(turn.start_ms)) && Number.isFinite(Number(turn.end_ms)))
          .sort((a, b) => Number(a.start_ms) - Number(b.start_ms));
      } else {
        const diarization = await fetchJson(`/spaces/${encodeURIComponent(spaceId)}/diarization`);
        if (playerSpeakerSpaceId !== spaceId) return;
        const intervals = diarization?.timeline?.intervals;
        if (Array.isArray(intervals) && intervals.length) {
          playerSpeakerSourceLabel = 'الفصل الصوتي المحلي';
          playerSpeakerTurns = intervals.map((interval, index) => {
            const id = String(interval.speaker_id || `SPEAKER_${String(index).padStart(2, '0')}`);
            const verifiedUsername = interval.username || '';
            const number = id.match(/([0-9]+)$/)?.[1];
            const label = verifiedUsername ? `@${verifiedUsername}` : `المتحدث ${number ? Number(number) + 1 : index + 1}`;
            return {
              start_ms: Number(interval.start) * 1000,
              end_ms: Number(interval.end) * 1000,
              username: verifiedUsername,
              display_name: label,
              role: 'speaker',
              speaker_id: id,
            };
          }).filter(turn => Number.isFinite(turn.start_ms) && Number.isFinite(turn.end_ms) && turn.end_ms > turn.start_ms);
        }
      }
      if (!playerSpeakerTurns.length) {
        resetPlayerSpeakerDisplay('لا توجد مداخلات متحدثين محفوظة');
        const count = document.getElementById('playerTurnsCount');
        if (count) count.textContent = 'لا توجد توقيتات محفوظة';
        const meta = document.getElementById('playerSpeakerMeta');
        if (meta) meta.textContent = 'يتطلب ذلك توفر رصد أو فصل صوتي لهذا التسجيل';
        return;
      }
      const note = document.getElementById('playerAudioNote');
      if (note) note.textContent = `مستوى الصوت للمقطع أثناء الحديث · تعريف المتحدث من ${playerSpeakerSourceLabel}`;
      renderPlayerSpeakerTrack();
      updatePlayerSpeakerAtTime(document.getElementById('mainVideoPlayer')?.currentTime || 0);
    }

    function speakerTrackColor(turn) {
      const palette = ['#1d9bf0', '#a78bfa', '#22c55e', '#f97316', '#ec4899', '#eab308', '#14b8a6', '#f43f5e'];
      const key = String(turn.username || turn.display_name || 'speaker');
      let hash = 0;
      for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
      return palette[hash % palette.length];
    }

    function seekPlayerToSpeaker(startMs) {
      const player = document.getElementById('mainVideoPlayer');
      if (!player) return;
      const seek = () => {
        player.currentTime = Math.max(0, Number(startMs) / 1000);
        player.play().catch(() => {});
      };
      if (player.readyState >= 1) seek();
      else player.addEventListener('loadedmetadata', seek, {once: true});
    }

    function renderPlayerSpeakerTrack() {
      const track = document.getElementById('playerSpeakerTrack');
      const axisTicks = document.getElementById('playerSpeakerAxisTicks');
      const count = document.getElementById('playerTurnsCount');
      if (!track || !axisTicks || !count) return;
      track.innerHTML = '';
      axisTicks.innerHTML = '';
      if (!playerSpeakerTurns.length) {
        count.textContent = 'لا توجد توقيتات محفوظة';
        track.innerHTML = '<div class="player-lanes-empty">لا توجد فترات كلام مسجلة لهذا التسجيل.</div>';
        return;
      }
      const player = document.getElementById('mainVideoPlayer');
      const durationMs = Number.isFinite(player?.duration) && player.duration > 0
        ? player.duration * 1000
        : Math.max(...playerSpeakerTurns.map(turn => Number(turn.end_ms) || 0));
      if (!durationMs) return;
      const groups = new Map();
      playerSpeakerTurns.forEach((turn, index) => {
        const key = String(turn.username || turn.speaker_id || turn.display_name || `speaker-${index}`);
        if (!groups.has(key)) groups.set(key, {key, turn, turns: [], color: speakerTrackColor(turn)});
        groups.get(key).turns.push(turn);
      });
      const avatarFor = (turn, label) => {
        const username = String(turn.username || '').replace(/^@/, '').toLowerCase();
        const account = username && allAccountsData.find(item => String(item.username || '').toLowerCase() === username);
        return account?.avatar_url || `https://ui-avatars.com/api/?name=${urlComponent(label)}&background=172033&color=fff&bold=true`;
      };
      const speakerValues = [...groups.values()];
      speakerValues.forEach(speaker => {
        const turn = speaker.turn;
        const label = turn.display_name || (turn.username ? `@${turn.username}` : 'متحدث');
        const role = turn.role === 'host' ? 'المضيف' : turn.role === 'cohost' ? 'مساعد المضيف' : 'متحدث';
        const row = document.createElement('div');
        row.className = 'player-speaker-lane';
        row.dataset.speakerKey = speaker.key;
        const person = document.createElement('div');
        person.className = 'player-lane-person';
        person.innerHTML = `<img class="player-lane-avatar" style="--speaker-color:${speaker.color}" src="${escapeHtml(avatarFor(turn, label))}" alt="صورة ${escapeHtml(label)}" onerror="this.onerror=null;this.src='https://ui-avatars.com/api/?name=${urlComponent(label)}&background=172033&color=fff&bold=true'"><span class="player-lane-copy"><strong class="player-lane-name">${escapeHtml(label)}</strong><span class="player-lane-role">${role}${turn.username ? ` · @${escapeHtml(turn.username)}` : ''} · ${speaker.turns.length} مداخلة</span></span>`;
        const lane = document.createElement('div');
        lane.className = 'player-lane-track';
        lane.dataset.speakerKey = speaker.key;
        speaker.turns.forEach(item => {
          const start = Math.max(0, Number(item.start_ms));
          const end = Math.min(durationMs, Number(item.end_ms));
          if (end <= start) return;
          const segment = document.createElement('button');
          segment.type = 'button';
          segment.className = 'player-lane-segment';
          segment.dataset.startMs = String(start);
          segment.dataset.endMs = String(end);
          segment.dataset.speakerKey = speaker.key;
          segment.style.left = `${(start / durationMs) * 100}%`;
          segment.style.width = `${Math.max(.35, ((end - start) / durationMs) * 100)}%`;
          segment.style.setProperty('--speaker-color', speaker.color);
          segment.textContent = formatSeconds((end - start) / 1000);
          segment.title = `${label} · ${formatSeconds(start / 1000)}–${formatSeconds(end / 1000)} · اضغط للاستماع`;
          segment.setAttribute('aria-label', segment.title);
          segment.addEventListener('click', () => seekPlayerToSpeaker(start));
          lane.appendChild(segment);
        });
        const playhead = document.createElement('span');
        playhead.className = 'player-lane-playhead';
        lane.appendChild(playhead);
        row.appendChild(person);
        row.appendChild(lane);
        track.appendChild(row);
      });
      [0, .25, .5, .75, 1].forEach(fraction => {
        const tick = document.createElement('span');
        tick.style.left = `${fraction * 100}%`;
        tick.textContent = formatSeconds(durationMs * fraction / 1000);
        axisTicks.appendChild(tick);
      });
      count.textContent = `${playerSpeakerTurns.length} مداخلة · ${speakerValues.length} متحدث`;
      updatePlayerTrackCursor(player?.currentTime || 0);
    }

    function updatePlayerTrackCursor(seconds) {
      const player = document.getElementById('mainVideoPlayer');
      const durationMs = Number.isFinite(player?.duration) && player.duration > 0
        ? player.duration * 1000
        : Math.max(0, ...playerSpeakerTurns.map(turn => Number(turn.end_ms) || 0));
      if (durationMs > 0) {
        const left = `${Math.min(100, Math.max(0, Number(seconds) * 1000 / durationMs * 100))}%`;
        document.querySelectorAll('.player-lane-playhead').forEach(cursor => { cursor.style.left = left; });
      }
      const at = Number(seconds) * 1000;
      document.querySelectorAll('#playerSpeakerTrack .player-lane-segment').forEach(node => {
        node.classList.toggle('is-active', at >= Number(node.dataset.startMs) && at < Number(node.dataset.endMs));
      });
    }

    function updatePlayerSpeakerAtTime(seconds) {
      const timeMs = Math.max(0, Number(seconds) * 1000);
      const turn = playerSpeakerTurns.find(item => timeMs >= Number(item.start_ms) && timeMs < Number(item.end_ms));
      const strip = document.getElementById('playerSpeakerStrip');
      const name = document.getElementById('playerSpeakerName');
      const meta = document.getElementById('playerSpeakerMeta');
      if (!turn) {
        resetPlayerSpeakerDisplay();
        updatePlayerTrackCursor(seconds);
        return null;
      }
      if (name) name.textContent = turn.display_name || (turn.username ? `@${turn.username}` : 'متحدث');
      if (meta) {
        const role = turn.role === 'host' ? 'المضيف' : turn.role === 'cohost' ? 'مساعد المضيف' : 'متحدث';
        meta.textContent = `${role}${turn.username ? ` · @${turn.username}` : ''} · ${formatSeconds(Math.max(0, (Number(turn.end_ms) - timeMs) / 1000))} متبقية`;
      }
      if (strip) strip.classList.add('is-speaking');
      updatePlayerTrackCursor(seconds);
      return turn;
    }

    function initializePlayerAudioBars() {
      const bars = document.getElementById('playerAudioBars');
      if (bars && !bars.childElementCount) {
        bars.innerHTML = Array.from({length: 28}, () => '<span class="player-audio-bar"></span>').join('');
      }
    }

    function preparePlayerAudioMeter() {
      const player = document.getElementById('mainVideoPlayer');
      const state = document.getElementById('playerAudioState');
      if (!player) return;
      initializePlayerAudioBars();
      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) throw new Error('Web Audio unavailable');
        if (!playerAudioContext) {
          playerAudioContext = new AudioContextClass();
          playerAudioAnalyser = playerAudioContext.createAnalyser();
          playerAudioAnalyser.fftSize = 256;
          playerAudioSource = playerAudioContext.createMediaElementSource(player);
          playerAudioSource.connect(playerAudioAnalyser);
          playerAudioAnalyser.connect(playerAudioContext.destination);
        }
        if (playerAudioContext.state === 'suspended') playerAudioContext.resume().catch(() => {});
        if (state) state.textContent = 'مستوى الصوت';
        return true;
      } catch (_) {
        if (state) state.textContent = 'مؤشر بصري';
        return false;
      }
    }

    function startPlayerAudioMeter() {
      const state = document.getElementById('playerAudioState');
      if (preparePlayerAudioMeter()) runPlayerAudioMeter();
      else {
        if (state) state.textContent = 'مؤشر بصري';
        animateSpeakerBarsFallback();
      }
    }

    function runPlayerAudioMeter() {
      if (!playerAudioAnalyser) return;
      cancelAnimationFrame(playerAudioFrame);
      const frequency = new Uint8Array(playerAudioAnalyser.frequencyBinCount);
      const bars = [...(document.getElementById('playerAudioBars')?.children || [])];
      const levelEl = document.getElementById('playerAudioLevel');
      const player = document.getElementById('mainVideoPlayer');
      const draw = () => {
        if (!player || player.paused || player.ended || document.getElementById('videoModal')?.classList.contains('hidden')) {
          playerAudioFrame = 0;
          setPlayerAudioBarsIdle();
          return;
        }
        playerAudioAnalyser.getByteFrequencyData(frequency);
        const usableBins = Math.max(1, Math.floor(frequency.length * .72));
        const timeMs = player.currentTime * 1000;
        const speaking = playerSpeakerTurns.some(turn => timeMs >= Number(turn.start_ms) && timeMs < Number(turn.end_ms));
        const values = bars.map((bar, index) => {
          const bin = Math.min(usableBins - 1, Math.floor((index / Math.max(1, bars.length - 1)) * usableBins));
          const value = speaking ? frequency[bin] / 255 : 0;
          const height = Math.max(3, Math.round(3 + value * 31));
          bar.style.height = `${height}px`;
          bar.style.opacity = String(.35 + value * .65);
          return value;
        });
        const peak = values.length ? Math.max(...values) : 0;
        playerAudioSmoothLevel = playerAudioSmoothLevel * .68 + peak * .32;
        if (levelEl) levelEl.textContent = `${Math.round(playerAudioSmoothLevel * 100)}%`;
        playerAudioFrame = requestAnimationFrame(draw);
      };
      playerAudioFrame = requestAnimationFrame(draw);
    }

    function animateSpeakerBarsFallback() {
      const bars = [...(document.getElementById('playerAudioBars')?.children || [])];
      bars.forEach((bar, index) => {
        bar.style.height = `${4 + ((index * 7) % 17)}px`;
      });
    }

    function setPlayerAudioBarsIdle() {
      const level = document.getElementById('playerAudioLevel');
      if (level) level.textContent = '0%';
      document.querySelectorAll('#playerAudioBars .player-audio-bar').forEach(bar => {
        bar.style.height = '3px';
        bar.style.opacity = '.35';
      });
    }

    let videoPreparationToken = 0;
    async function waitForVideoPreparation(spaceId, token) {
      const statusUrl = `/spaces/${encodeURIComponent(spaceId)}/media-status`;
      let requested = false;
      for (let attempt = 0; attempt < 120; attempt++) {
        if (token !== videoPreparationToken) return false;
        const response = await fetch(statusUrl, {cache:'no-store'});
        if (!response.ok) throw Error(`HTTP ${response.status}`);
        const media = await response.json();
        if (media.playback_ready) return true;
        if (media.status === 'unavailable') throw Error('ملف التسجيل المصدر غير متاح');
        if (media.status === 'failed') throw Error(media.error || 'تعذر تجهيز الفيديو');
        if (media.status === 'needs_preparation' && !requested) {
          const queued = await fetch(`/spaces/${encodeURIComponent(spaceId)}/prepare-media`, {method:'POST'});
          if (!queued.ok) throw Error(`HTTP ${queued.status}`);
          requested = true;
        }
        document.getElementById('playerAudioState').textContent = 'يجري تجهيز الفيديو في الخلفية…';
        await new Promise(resolve => setTimeout(resolve, 5000));
      }
      throw Error('لا يزال تجهيز الفيديو جاريًا؛ افتح التسجيل لاحقًا');
    }

    async function openVideoModal(spaceId, encodedTitle, host) {
      const token = ++videoPreparationToken;
      const modal = document.getElementById('videoModal');
      const player = document.getElementById('mainVideoPlayer');
      const title = decodeURIComponent(encodedTitle || 'معاينة المساحة');

      document.getElementById('videoModalTitle').innerText = title;
      document.getElementById('videoModalSubtitle').innerText = `المضيف: @${host} | معرف المساحة: ${spaceId}`;
      document.getElementById('videoDownloadBtn').href = `/spaces/${spaceId}/video?download=true`;
      document.getElementById('videoOpenSpaceBtn').href = `https://x.com/i/spaces/${spaceId}`;

      player.pause();
      player.muted = false;
      player.volume = 1.0;
      player.playbackRate = 1;
      playerAudioSmoothLevel = 0;
      initializePlayerAudioBars();
      setPlayerAudioBarsIdle();
      document.getElementById('playerAudioState').textContent = 'بانتظار التشغيل';
      document.getElementById('videoPlaybackRate').value = '1';
      document.getElementById('videoSeekSlider').value = '0';
      modal.classList.remove('hidden');
      loadPlayerSpeakerTimeline(spaceId);

      preparePlayerAudioMeter();
      try {
        if (!await waitForVideoPreparation(spaceId, token) || token !== videoPreparationToken) return;
      } catch (error) {
        if (token === videoPreparationToken) document.getElementById('playerAudioState').textContent = error.message;
        return;
      }
      player.src = `/spaces/${encodeURIComponent(spaceId)}/video`;
      player.load();
      const p = player.play();
      if (p !== undefined) {
        p.catch(err => {
          console.log('Autoplay handled by browser:', err);
        });
      }
    }

    function unmuteAndMaxVolume() {
      const player = document.getElementById('mainVideoPlayer');
      if (player) {
        player.muted = false;
        player.volume = 1.0;
        const slider = document.getElementById('videoVolumeSlider');
        if (slider) slider.value = '1';
        const percent = document.getElementById('volumePercent');
        if (percent) percent.innerText = '100%';
        player.play().catch(() => {});
      }
    }

    function toggleVideoPlay() {
      const p = document.getElementById('mainVideoPlayer');
      const ic = document.getElementById('iconModalPlay');
      if (!p) return;
      if (p.paused) {
        p.play().catch(() => {});
        if (ic) ic.className = 'fa-solid fa-pause text-xs';
      } else {
        p.pause();
        if (ic) ic.className = 'fa-solid fa-play text-xs';
      }
    }

    function skipVideo(sec) {
      const p = document.getElementById('mainVideoPlayer');
      if (p) p.currentTime = Math.max(0, p.currentTime + sec);
    }

    function seekVideo(value) {
      const p = document.getElementById('mainVideoPlayer');
      if (p && Number.isFinite(p.duration) && p.duration > 0) {
        p.currentTime = (Number(value) / 1000) * p.duration;
      }
    }

    function changePlaybackRate(value) {
      const p = document.getElementById('mainVideoPlayer');
      if (p) p.playbackRate = Number(value) || 1;
    }

    function changeVideoVolume(val) {
      const p = document.getElementById('mainVideoPlayer');
      if (p) {
        p.volume = parseFloat(val);
        p.muted = false;
        const percent = document.getElementById('volumePercent');
        if (percent) percent.innerText = Math.round(parseFloat(val) * 100) + '%';
      }
    }

    function toggleFullscreen() {
      const stage = document.getElementById('videoPlayerStage');
      if (!stage) return;
      if (!document.fullscreenElement) {
        stage.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen().catch(() => {});
      }
    }

    // Attach listeners on load
    window.addEventListener('DOMContentLoaded', () => {
      const p = document.getElementById('mainVideoPlayer');
      if (p) {
        p.addEventListener('timeupdate', () => {
          const cur = formatSeconds(p.currentTime);
          const dur = formatSeconds(p.duration || 0);
          const disp = document.getElementById('videoTimeDisplay');
          if (disp) disp.innerText = `${cur} / ${dur}`;
          const seek = document.getElementById('videoSeekSlider');
          if (seek && Number.isFinite(p.duration) && p.duration > 0) seek.value = String(Math.round((p.currentTime / p.duration) * 1000));
          updatePlayerSpeakerAtTime(p.currentTime);
        });
        p.addEventListener('loadedmetadata', () => {
          const disp = document.getElementById('videoTimeDisplay');
          if (disp) disp.innerText = `00:00 / ${formatSeconds(p.duration || 0)}`;
          renderPlayerSpeakerTrack();
        });
        p.addEventListener('play', () => {
          const ic = document.getElementById('iconModalPlay');
          if (ic) ic.className = 'fa-solid fa-pause text-xs';
          startPlayerAudioMeter();
        });
        p.addEventListener('pause', () => {
          const ic = document.getElementById('iconModalPlay');
          if (ic) ic.className = 'fa-solid fa-play text-xs';
          cancelAnimationFrame(playerAudioFrame);
          playerAudioFrame = 0;
          setPlayerAudioBarsIdle();
          const state = document.getElementById('playerAudioState');
          if (state) state.textContent = 'متوقف مؤقتًا';
        });
      }
    });

    function closeVideoModal() {
      videoPreparationToken++;
      const modal = document.getElementById('videoModal');
      const player = document.getElementById('mainVideoPlayer');
      if (player) {
        player.pause();
        player.src = '';
      }
      cancelAnimationFrame(playerAudioFrame);
      playerAudioFrame = 0;
      playerSpeakerSpaceId = '';
      playerSpeakerTurns = [];
      resetPlayerSpeakerDisplay('حمّل تسجيلًا لعرض المتحدث');
      setPlayerAudioBarsIdle();
      if (modal) {
        modal.classList.add('hidden');
      }
    }

    // --- Space Inspector Drawer ---
    async function openInspector(spaceId) {
      currentSpaceId = spaceId;
      const card = document.getElementById('inspectorCard');
      card.classList.remove('hidden');
      card.scrollIntoView({ behavior: 'smooth' });

      document.getElementById('insSpaceId').innerText = 'ID: ' + spaceId;
      const stats = await fetchJson(`/spaces/${spaceId}/stats`);
      if (stats) {
        document.getElementById('insTitle').innerText = stats.title || 'مساحة بدون عنوان';
        document.getElementById('insHost').innerText = 'المضيف: @' + (stats.host || 'unknown');
      }

      loadTimeline(spaceId);
      loadTranscript(spaceId);
      loadSummary(spaceId);
    }

    function closeInspector() {
      document.getElementById('inspectorCard').classList.add('hidden');
      currentSpaceId = null;
    }

    function switchTab(tabId) {
      ['tabTimeline', 'tabTranscript', 'tabSummary'].forEach(t => {
        document.getElementById(t).classList.add('hidden');
      });
      document.getElementById(tabId).classList.remove('hidden');
      ['btnTabTimeline', 'btnTabTranscript', 'btnTabSummary'].forEach(b => {
        document.getElementById(b).className = 'pb-2 text-gray-400 hover:text-white';
      });
      const activeBtn = tabId === 'tabTimeline' ? 'btnTabTimeline' :
                        tabId === 'tabTranscript' ? 'btnTabTranscript' : 'btnTabSummary';
      document.getElementById(activeBtn).className = 'pb-2 text-xblue border-b-2 border-xblue';
    }

    async function loadTimeline(spaceId) {
      const data = await fetchJson(`/spaces/${spaceId}/speaker-timeline`);
      const container = document.getElementById('timelineBars');
      const countEl = document.getElementById('timelineTotalEvents');

      if (data && data.space) {
        document.getElementById('insTitle').innerText = data.space.title || 'مساحة بدون عنوان';
        document.getElementById('insHost').innerText = 'المضيف: @' + (data.space.host || 'unknown');
        const url = document.getElementById('insSpaceUrl');
        document.getElementById('insTimelineDownload').href = `/spaces/${spaceId}/speaker-timeline.json`;
        try {
          const parsed = new URL(data.space.url);
          if (parsed.protocol === 'https:' && ['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(parsed.hostname)) {
            url.href = parsed.href;
          } else {
            url.href = '#';
          }
        } catch (_) { url.href = '#'; }
        const when = data.space.recording_started_at || data.space.detected_at;
        document.getElementById('insRecordingTime').innerText = when
          ? `وقت التسجيل: ${new Date(when).toLocaleString('ar-SA', { timeZone: 'Asia/Riyadh' })}` : '';
        document.getElementById('insFolderPath').innerText = data.space.folder || '';
        window._currentInspectorMeta = { space_id: spaceId, title: data.space.title || '', host: data.space.host || 'unknown' };
      }

      if (!data || !data.speakers || data.speakers.length === 0) {
        container.innerHTML = '<div class="text-xs text-gray-500 py-4 text-center">لا توجد أحداث متحدثين مسجلة بعد</div>';
        countEl.innerText = '0 مداخلة';
        return;
      }

      countEl.innerText = `${data.speakers.length} متحدث · ${data.turn_count} مداخلة`;
      const totalTime = data.speakers.reduce((sum, speaker) => sum + speaker.speaking_ms, 0);
      const summaries = data.speakers.map(speaker => {
        const pct = totalTime > 0 ? Math.round((speaker.speaking_ms / totalTime) * 100) : 0;
        return `
          <div class="space-y-1">
            <div class="flex justify-between text-xs font-semibold">
              <span class="text-white">${escapeHtml(speaker.display_name || speaker.username)} <span class="dir-ltr text-gray-400">@${escapeHtml(speaker.username)}</span></span>
              <span class="text-gray-400 font-mono">${formatSeconds(speaker.speaking_ms / 1000)} · ${speaker.turn_count} مداخلة · ${pct}%</span>
            </div>
            <div class="h-2 w-full bg-gray-800 rounded-full overflow-hidden">
              <div class="h-full bg-gradient-to-r from-xblue to-xpurple rounded-full" style="width: ${pct}%"></div>
            </div>
          </div>
        `;
      }).join('');
      const turns = data.turns.slice(0, 200).map(turn => `
        <div class="flex items-center justify-between gap-3 rounded-lg border border-xborder/70 bg-black/30 px-3 py-2 text-xs">
          <div class="min-w-0">
            <div class="font-semibold text-white truncate">${escapeHtml(turn.display_name || turn.username)} <span class="dir-ltr text-gray-500">@${escapeHtml(turn.username)}</span></div>
            <div class="mt-0.5 text-gray-400">${turn.start_at ? new Date(turn.start_at).toLocaleTimeString('ar-SA', { timeZone: 'Asia/Riyadh' }) : '—'} · ${formatSeconds(turn.start_ms / 1000)} → ${formatSeconds(turn.end_ms / 1000)} · ${formatSeconds(turn.duration_ms / 1000)}${turn.is_open ? ' · لم تُرصد نهاية المداخلة' : ''}</div>
          </div>
          <button onclick="${inlineCall('seekToSpeakerTurn', [spaceId, turn.start_ms])}" class="shrink-0 rounded-lg bg-xblue/15 px-2.5 py-1.5 text-xblue hover:bg-xblue/25" title="الانتقال إلى بداية المداخلة في التسجيل">
            <i class="fa-solid fa-play ml-1"></i>استمع
          </button>
        </div>
      `).join('');
      const note = data.attribution_note ? `<p class="text-[10px] text-gray-500">${escapeHtml(data.attribution_note)}</p>` : '';
      container.innerHTML = `<div class="space-y-3">${summaries}<div class="border-t border-xborder pt-3 space-y-2"><h5 class="text-[11px] font-bold text-gray-300">المداخلات بالترتيب الزمني</h5>${turns || '<p class="text-xs text-gray-500">لا توجد فترات حديث مكتملة بعد.</p>'}${data.turns.length > 200 ? `<p class="text-[10px] text-gray-500">يُعرض أول 200 من ${data.turns.length} مداخلة.</p>` : ''}</div>${note}</div>`;
    }

    function seekToSpeakerTurn(spaceId, startMs) {
      const meta = window._currentInspectorMeta || { space_id: spaceId, title: '', host: 'unknown' };
      const player = document.getElementById('mainVideoPlayer');
      const seek = () => {
        player.currentTime = Math.max(0, Number(startMs) / 1000);
        player.play().catch(() => {});
      };
      if (player.currentSrc && player.currentSrc.includes(`/spaces/${spaceId}/video`) && player.readyState >= 1) {
        document.getElementById('videoModal').classList.remove('hidden');
        seek();
        return;
      }
      player.addEventListener('loadedmetadata', seek, { once: true });
      openVideoModal(spaceId, urlComponent(meta.title), meta.host);
    }

    async function loadTranscript(spaceId) {
      let data = await fetchJson(`/spaces/${encodeURIComponent(spaceId)}/transcript`);
      const container = document.getElementById('transcriptList');
      if (data?.recognition_state === 'NO_SPEECH') {
        window._currentSegments = [];
        window._transcriptPartial = false;
        container.innerHTML = '<div class="text-xs text-gray-400 py-6 text-center">اكتمل التفريغ ولم يُرصد كلام في التسجيل.</div>';
        return;
      }
      if (!data || !data.segments || data.segments.length === 0) {
        const partial = await fetchJson(`/spaces/${encodeURIComponent(spaceId)}/transcript/partial`);
        if (partial.recognition_state === 'PARTIAL') {
          data = partial;
        } else {
          container.innerHTML = '<div class="text-xs text-gray-500 py-6 text-center">لم يبدأ التفريغ أو لا توجد نتيجة محفوظة بعد.</div>';
          return;
        }
      }
      window._currentSegments = data.segments;
      window._transcriptPartial = data.recognition_state === 'PARTIAL';
      window._transcriptWindowStatus = `${data.completed_windows} / ${data.total_windows}`;
      renderSegments(data.segments, window._transcriptPartial);
    }

    function renderSegments(segments, partial = false) {
      const container = document.getElementById('transcriptList');
      const notice = partial ? `<div class="p-3 mb-3 rounded-xl border border-amber-500/30 bg-amber-500/10 text-amber-200 text-xs">تفريغ جزئي · النوافذ المكتملة: ${window._transcriptWindowStatus}. النص قابل للتغيير ولم يُنشر كنسخة نهائية.</div>` : '';
      container.innerHTML = notice + segments.map(s => `
        <button type="button" onclick="seekToSpeakerTurn(currentSpaceId, ${Math.max(0, Number(s.start) * 1000 || 0)})" class="block w-full text-right p-3 rounded-xl bg-black/40 border border-xborder text-xs space-y-1 hover:border-xblue transition-colors">
          <div class="flex items-center justify-between text-gray-400 text-[11px]">
            <span class="font-bold text-xblue dir-ltr">${s.speaker ? '@' + escapeHtml(s.speaker) : 'متحدث غير محدد'}</span>
            <span class="font-mono">${formatSeconds(s.start)}</span>
          </div>
          <p class="text-gray-200 leading-relaxed">${escapeHtml(s.text)}</p>
        </button>
      `).join('');
    }

    function filterTranscript() {
      const q = document.getElementById('transcriptSearch').value.toLowerCase();
      if (!window._currentSegments) return;
      const filtered = window._currentSegments.filter(s => String(s.text || '').toLowerCase().includes(q));
      renderSegments(filtered, window._transcriptPartial);
    }

    async function loadSummary(spaceId) {
      const data = await fetchJson(`/spaces/${spaceId}/summary`);
      if (!data) return;
      document.getElementById('summaryText').innerText = data.executive_summary || 'لم يتم توليد ملخص بعد.';
      const topicsContainer = document.getElementById('topicsTags');
      if (data.key_topics && data.key_topics.length > 0) {
        topicsContainer.innerHTML = data.key_topics.map(t => `
          <span class="px-2.5 py-1 rounded-lg bg-xpurple/20 text-xpurple border border-xpurple/30 text-xs font-semibold">
            #${escapeHtml(t)}
          </span>
        `).join('');
      }
      const deep = data.deep_insights || {};
      const participation = deep.participation || {};
      document.getElementById('deepParticipation').innerHTML = participation.label
        ? `${escapeHtml(participation.label)} · ${Number(participation.speaker_count || 0)} متحدث · الأكثر مشاركة: ${escapeHtml(participation.dominant_speaker || 'غير محدد')} (${Number(participation.dominant_share_percent || 0)}%) · مقاطع بلا متحدث محدد: ${Number(participation.unknown_speaker_share_percent || 0)}%`
        : 'لا توجد مؤشرات مشاركة بعد.';
      const takeaways = Array.isArray(data.key_takeaways) ? data.key_takeaways : [];
      document.getElementById('deepTakeaways').innerHTML = takeaways.length
        ? `<h5 class="text-[11px] font-bold text-gray-300">الخلاصات</h5>${takeaways.slice(0,6).map(item => `<p class="text-[11px] text-gray-400">• ${escapeHtml(typeof item === 'string' ? item : (item.text || item.title || JSON.stringify(item)))}</p>`).join('')}`
        : '';
      const unresolved = Array.isArray(data.open_questions) ? data.open_questions : [];
      document.getElementById('deepOpenQuestions').innerHTML = unresolved.length
        ? `<h5 class="text-[11px] font-bold text-gray-300">نقاط بقيت بلا حسم حسب التحليل</h5>${unresolved.slice(0,6).map(item => `<p class="text-[11px] text-gray-400">• ${escapeHtml(typeof item === 'string' ? item : (item.text || item.question || JSON.stringify(item)))}</p>`).join('')}`
        : '';
      const evidence = Array.isArray(deep.topic_evidence) ? deep.topic_evidence : [];
      document.getElementById('deepTopics').innerHTML = evidence.length
        ? `<h5 class="text-[11px] font-bold text-gray-300">موضوعات مع شواهد نصية</h5>${evidence.slice(0,6).map(item => `<div class="rounded-lg bg-gray-900/70 p-2"><div class="text-xpurple text-xs font-bold">${escapeHtml(item.topic)} <span class="text-gray-500 font-normal">${Number(item.mentions || 0)} ذكر</span></div>${(item.evidence || []).slice(0,2).map(ev => `<button onclick="seekToSpeakerTurn(currentSpaceId, ${Number(ev.start || 0) * 1000})" class="block text-right text-[11px] text-gray-400 hover:text-white mt-1">${escapeHtml(ev.timestamp || '')} · ${escapeHtml(ev.speaker || 'غير محدد')}: ${escapeHtml(ev.excerpt || '')}</button>`).join('')}</div>`).join('')}`
        : '<p class="text-[11px] text-gray-500">لم تتكرر كلمات كافية لاستخراج موضوعات مدعومة.</p>';
      const moments = Array.isArray(deep.key_moments) ? deep.key_moments : [];
      document.getElementById('deepMoments').innerHTML = moments.length
        ? `<h5 class="text-[11px] font-bold text-gray-300">مقاطع بارزة حسب طول التفريغ</h5>${moments.slice(0,5).map(item => `<button onclick="seekToSpeakerTurn(currentSpaceId, ${Number(item.start || 0) * 1000})" class="block w-full text-right rounded-lg bg-gray-900/70 p-2 text-[11px] text-gray-400 hover:text-white">${escapeHtml(item.timestamp || '')} · ${escapeHtml(item.speaker || 'غير محدد')}: ${escapeHtml(item.excerpt || '')}</button>`).join('')}`
        : '';
      const qa = Array.isArray(deep.question_response_leads) ? deep.question_response_leads : [];
      document.getElementById('deepQuestions').innerHTML = qa.length
        ? `<h5 class="text-[11px] font-bold text-gray-300">أسئلة وردود محتملة (تحتاج مراجعة)</h5>${qa.slice(0,6).map(item => `<div class="rounded-lg bg-gray-900/70 p-2 text-[11px]"><button onclick="seekToSpeakerTurn(currentSpaceId, ${Number(item.start || 0) * 1000})" class="text-sky-300">${escapeHtml(item.timestamp_str || '')} · ${escapeHtml(item.speaker || 'متحدث')}: ${escapeHtml(item.question || '')}</button><div class="text-gray-400 mt-1">رد محتمل من ${escapeHtml(item.response_speaker || 'متحدث آخر')}: ${escapeHtml(item.possible_response || '')}</div></div>`).join('')}`
        : '';
      document.getElementById('deepCaveats').innerText = (deep.limitations || []).join(' ');
    }

    async function quickTranscribe(spaceId) {
      alert('تم بدء عملية التفريغ الصوتي في الخلفية...');
      await fetchJson(`/spaces/${spaceId}/transcribe`, { method: 'POST' });
      loadSpaces();
    }

    async function quickAnalyze(spaceId) {
      alert('تم بدء التحليل بالذكاء الاصطناعي...');
      await fetchJson(`/spaces/${spaceId}/analyze`, { method: 'POST' });
      loadSpaces();
    }

    async function retrySpaceProcessing(spaceId) {
      const result = await fetchJson(`/spaces/${spaceId}/process`, { method: 'POST' });
      if (result) {
        alert('تمت جدولة إعادة معالجة التسجيل.');
        loadSpaces();
      }
    }

    async function stopRecording(spaceId) {
      if (!confirm(`إيقاف تسجيل المساحة ${spaceId}؟ سيُحفظ الملف ويُوسم التسجيل بأنه أُوقف يدوياً.`)) return;
      const result = await fetchJson(`/admin/recordings/${encodeURIComponent(spaceId)}/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'إيقاف يدوي من لوحة الإدارة' })
      });
      if (result) {
        updateLiveStatus();
        loadSpaces();
      } else {
        alert(`تعذر إيقاف التسجيل: ${readableApiError()}`);
      }
    }

    async function cancelProcessing(spaceId) {
      if (!confirm(`إلغاء معالجة المساحة ${spaceId}؟`)) return;
      const result = await fetchJson(`/admin/spaces/${encodeURIComponent(spaceId)}/processing`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'أُلغيَت المعالجة من لوحة الإدارة' })
      });
      if (result) loadSpaces();
      else alert(`تعذر إلغاء المعالجة: ${readableApiError()}`);
    }

    async function quarantineSpace(spaceId) {
      if (!confirm(`عزل المساحة ${spaceId}؟ سيُمنع تسجيلها تلقائياً وتبقى ملفاتها محفوظة.`)) return;
      const result = await fetchJson(`/admin/spaces/${encodeURIComponent(spaceId)}/quarantine`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'عزل يدوي من لوحة الإدارة' })
      });
      if (result) loadSpaces();
      else alert(`تعذر عزل المساحة: ${readableApiError()}`);
    }

    async function triggerTranscribe() {
      if (!currentSpaceId) return;
      await quickTranscribe(currentSpaceId);
      loadTranscript(currentSpaceId);
    }

    async function triggerAnalyze() {
      if (!currentSpaceId) return;
      await quickAnalyze(currentSpaceId);
      loadSummary(currentSpaceId);
    }

    function downloadArtifact(type) {
      if (!currentSpaceId) return;
      window.open(`/spaces/${currentSpaceId}/transcript/${type}`, '_blank');
    }

    // Init
    document.addEventListener('keydown', (event) => {
      const target = event.target;
      const isTyping = target instanceof HTMLElement && (
        target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
      );
      if (event.key === '/' && !isTyping && !event.ctrlKey && !event.metaKey && !event.altKey) {
        const search = document.getElementById('librarySearchInput');
        if (search) {
          event.preventDefault();
          search.focus();
        }
      } else if (event.key === 'Escape') {
        if (!document.getElementById('videoModal').classList.contains('hidden')) closeVideoModal();
        else if (!document.getElementById('spaceActionModal').classList.contains('hidden')) closeSpaceActionModal();
        else if (!document.getElementById('tagsModal').classList.contains('hidden')) closeTagsModal();
        else if (!document.getElementById('inspectorCard').classList.contains('hidden')) closeInspector();
      }
    });
    observeDashboardKpis();
    loadAccounts();
    loadSpaces();
    updateLiveStatus();
    updateSystemHealth();
    loadProcessingJobs();
    updateLiveFeed();
    setInterval(updateLiveStatus, 3000);
    setInterval(updateSystemHealth, 10000);
    setInterval(loadProcessingJobs, 20000);
    setInterval(updateLiveFeed, 15000);
    setInterval(() => {
      const section = document.getElementById('librarySection');
      if (document.visibilityState !== 'visible' || !section) return;
      const bounds = section.getBoundingClientRect();
      if (bounds.bottom > 0 && bounds.top < window.innerHeight) loadSpaces();
    }, 15000);
    setInterval(loadAccounts, 30000);
  