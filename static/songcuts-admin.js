document.addEventListener("DOMContentLoaded", () => {
    const player = document.getElementById("mainPlayer");
    const currentTitle = document.getElementById("currentTitle");
    const currentMeta = document.getElementById("currentMeta");
    const categoryFilter = document.getElementById("categoryFilter");
    const searchInput = document.getElementById("searchInput");
    const songList = document.getElementById("songList");
    const emptyState = document.getElementById("emptyState");
    const resultSummary = document.getElementById("resultSummary");
    const prevButton = document.getElementById("prevButton");
    const nextButton = document.getElementById("nextButton");
    const toggleButton = document.getElementById("toggleButton");
    const randomButton = document.getElementById("randomButton");
    const playbackMode = document.getElementById("playbackMode");
    const modeHint = document.getElementById("modeHint");

    const extractForm = document.getElementById("extractForm");
    const extractButton = document.getElementById("extractButton");
    const sourceFileInput = document.getElementById("sourceFileInput");
    const categoryInput = document.getElementById("categoryInput");
    const categorySuggestions = document.getElementById("categorySuggestions");
    const outputFormatSelect = document.getElementById("outputFormatSelect");
    const minDurationInput = document.getElementById("minDurationInput");
    const maxSilenceInput = document.getElementById("maxSilenceInput");
    const leadingPaddingInput = document.getElementById("leadingPaddingInput");
    const trailingPaddingInput = document.getElementById("trailingPaddingInput");
    const activeRatioInput = document.getElementById("activeRatioInput");
    const ffmpegPathInput = document.getElementById("ffmpegPathInput");
    const ffmpegHint = document.getElementById("ffmpegHint");
    const extractStatus = document.getElementById("extractStatus");
    const extractResults = document.getElementById("extractResults");

    const saveBrecConfigButton = document.getElementById("saveBrecConfigButton");
    const refreshBrecButton = document.getElementById("refreshBrecButton");
    const brecApiUrlInput = document.getElementById("brecApiUrlInput");
    const brecWorkdirInput = document.getElementById("brecWorkdirInput");
    const brecUsernameInput = document.getElementById("brecUsernameInput");
    const brecPasswordInput = document.getElementById("brecPasswordInput");
    const brecWebhookSecretInput = document.getElementById("brecWebhookSecretInput");
    const brecAutoExtractInput = document.getElementById("brecAutoExtractInput");
    const brecAutoCategoryInput = document.getElementById("brecAutoCategoryInput");
    const brecStatus = document.getElementById("brecStatus");
    const brecWebhookBox = document.getElementById("brecWebhookBox");
    const brecRooms = document.getElementById("brecRooms");
    const brecRecordings = document.getElementById("brecRecordings");

    const PLAYBACK_MODE_STORAGE_KEY = "songcuts-playback-mode";
    const playbackModeText = {
        pause: "播完暂停",
        next: "播完自动下一首",
        random: "播完随机播放",
    };

    let allSongs = [];
    let filteredSongs = [];
    let currentSongPath = null;

    initializePlaybackMode();
    bindEvents();
    loadExtractorInfo();
    loadSongcuts();
    loadBrecSummary();

    function bindEvents() {
        categoryFilter.addEventListener("change", applyFilters);
        searchInput.addEventListener("input", applyFilters);
        prevButton.addEventListener("click", () => playRelative(-1));
        nextButton.addEventListener("click", () => playRelative(1));
        toggleButton.addEventListener("click", togglePlayback);
        randomButton.addEventListener("click", playRandomSong);
        playbackMode.addEventListener("change", handlePlaybackModeChange);
        player.addEventListener("ended", handlePlaybackEnded);
        extractForm.addEventListener("submit", handleExtractSubmit);
        saveBrecConfigButton.addEventListener("click", saveBrecConfig);
        refreshBrecButton.addEventListener("click", loadBrecSummary);
    }

    function initializePlaybackMode() {
        const storedMode = window.localStorage.getItem(PLAYBACK_MODE_STORAGE_KEY);
        const initialMode = playbackModeText[storedMode] ? storedMode : "pause";
        playbackMode.value = initialMode;
        updateModeHint(initialMode);
    }

    async function fetchJson(url, options = {}) {
        const response = await fetch(url, options);
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) {
            window.location.href = "/admin/login";
            throw new Error("登录已失效，请重新进入后台。");
        }
        if (!response.ok) {
            throw new Error(data.detail || data.message || `请求失败: ${response.status}`);
        }
        return data;
    }

    function getCurrentExtractionOptions() {
        return {
            category: categoryInput.value.trim() || "自动提取",
            output_format: outputFormatSelect.value,
            min_duration: Number(minDurationInput.value),
            max_silence: Number(maxSilenceInput.value),
            leading_padding: Number(leadingPaddingInput.value),
            trailing_padding: Number(trailingPaddingInput.value),
            min_active_ratio: Number(activeRatioInput.value),
            ffmpeg_path: ffmpegPathInput.value.trim(),
        };
    }

    function handlePlaybackModeChange() {
        const selectedMode = playbackMode.value;
        window.localStorage.setItem(PLAYBACK_MODE_STORAGE_KEY, selectedMode);
        updateModeHint(selectedMode);
    }

    function updateModeHint(mode) {
        modeHint.textContent = `当前模式：${playbackModeText[mode] || playbackModeText.pause}`;
    }

    function handlePlaybackEnded() {
        if (!filteredSongs.length) {
            return;
        }
        if (playbackMode.value === "next") {
            playRelative(1);
            return;
        }
        if (playbackMode.value === "random") {
            playRandomSong();
        }
    }

    async function loadExtractorInfo() {
        ffmpegHint.textContent = "正在检查 ffmpeg...";

        try {
            const data = await fetchJson("/api/songcuts/extractor-info");
            const categories = data.songcut_categories || [];
            categorySuggestions.innerHTML = "";
            categories.forEach((category) => {
                const option = document.createElement("option");
                option.value = category;
                categorySuggestions.appendChild(option);
            });

            if (data.ffmpeg_available) {
                ffmpegHint.textContent = `已检测到 ffmpeg：${data.ffmpeg_path}`;
            } else {
                ffmpegHint.textContent = "未检测到 ffmpeg，请安装后放到常见目录，或手动填写路径。";
            }
        } catch (error) {
            console.error("加载提取器信息失败:", error);
            ffmpegHint.textContent = "无法获取 ffmpeg 状态，请确认后端是否正常运行。";
        }
    }

    async function loadSongcuts() {
        resultSummary.textContent = "正在加载歌切...";

        try {
            const data = await fetchJson("/api/songcuts");
            allSongs = flattenSongs(data.categories || {});
            populateCategories(allSongs);
            applyFilters();
        } catch (error) {
            console.error("加载歌切失败:", error);
            resultSummary.textContent = "歌切加载失败";
            songList.innerHTML = "";
            emptyState.hidden = false;
            emptyState.innerHTML = "<p>接口加载失败，请检查后端是否正常运行。</p>";
        }
    }

    function flattenSongs(categories) {
        const songs = [];
        Object.entries(categories).forEach(([category, items]) => {
            items.forEach((item, index) => {
                songs.push({
                    ...item,
                    category,
                    id: `${category}-${index}-${item.filename}`,
                });
            });
        });
        return songs;
    }

    function populateCategories(songs) {
        const categories = [...new Set(songs.map((song) => song.category))]
            .sort((left, right) => left.localeCompare(right, "zh-CN"));
        const currentValue = categoryFilter.value;

        categoryFilter.innerHTML = '<option value="">全部分类</option>';
        categories.forEach((category) => {
            const option = document.createElement("option");
            option.value = category;
            option.textContent = category;
            categoryFilter.appendChild(option);
        });

        if (categories.includes(currentValue)) {
            categoryFilter.value = currentValue;
        }
    }

    function applyFilters() {
        const selectedCategory = categoryFilter.value;
        const keyword = searchInput.value.trim().toLocaleLowerCase();

        filteredSongs = allSongs.filter((song) => {
            const matchCategory = !selectedCategory || song.category === selectedCategory;
            const haystack = `${song.title} ${song.filename} ${song.category}`.toLocaleLowerCase();
            return matchCategory && (!keyword || haystack.includes(keyword));
        });

        renderSongList();
        resultSummary.textContent = filteredSongs.length === allSongs.length
            ? `共 ${allSongs.length} 首歌切`
            : `筛选后 ${filteredSongs.length} / ${allSongs.length} 首歌切`;
    }

    function renderSongList() {
        songList.innerHTML = "";

        if (!filteredSongs.length) {
            emptyState.hidden = false;
            return;
        }

        emptyState.hidden = true;

        const grouped = filteredSongs.reduce((accumulator, song) => {
            if (!accumulator[song.category]) {
                accumulator[song.category] = [];
            }
            accumulator[song.category].push(song);
            return accumulator;
        }, {});

        Object.entries(grouped).forEach(([category, songs]) => {
            const group = document.createElement("section");
            group.className = "song-group";

            const header = document.createElement("div");
            header.className = "song-group-header";
            header.innerHTML = `<h3>${category}</h3><span class="song-count">${songs.length} 首</span>`;

            const items = document.createElement("div");
            items.className = "song-items";

            songs.forEach((song) => {
                const item = document.createElement("article");
                item.className = "song-item";
                if (song.path === currentSongPath) {
                    item.classList.add("active");
                }

                const copy = document.createElement("div");
                copy.className = "song-copy";
                copy.innerHTML = `
                    <div class="song-title">${song.title}</div>
                    <div class="song-meta">${song.filename}</div>
                `;

                const button = document.createElement("button");
                button.type = "button";
                button.className = "song-play";
                button.textContent = song.path === currentSongPath ? "正在播放" : "播放";
                button.addEventListener("click", () => playSong(song));

                item.appendChild(copy);
                item.appendChild(button);
                items.appendChild(item);
            });

            group.appendChild(header);
            group.appendChild(items);
            songList.appendChild(group);
        });
    }

    async function playSong(song) {
        currentSongPath = song.path;
        player.src = song.path;
        currentTitle.textContent = song.title;
        currentMeta.textContent = `${song.category} · ${song.filename}`;
        renderSongList();

        try {
            await player.play();
        } catch (error) {
            console.error("播放失败:", error);
            currentMeta.textContent = "浏览器阻止了自动播放，请再点一次播放。";
        }
    }

    function playRelative(offset) {
        if (!filteredSongs.length) {
            return;
        }

        const currentIndex = filteredSongs.findIndex((song) => song.path === currentSongPath);
        const safeIndex = currentIndex === -1 ? 0 : currentIndex;
        const nextIndex = (safeIndex + offset + filteredSongs.length) % filteredSongs.length;
        playSong(filteredSongs[nextIndex]);
    }

    function togglePlayback() {
        if (!player.src && filteredSongs.length) {
            playSong(filteredSongs[0]);
            return;
        }

        if (player.paused) {
            player.play().catch((error) => console.error("恢复播放失败:", error));
            return;
        }

        player.pause();
    }

    function playRandomSong() {
        if (!filteredSongs.length) {
            return;
        }

        const currentIndex = filteredSongs.findIndex((song) => song.path === currentSongPath);
        if (filteredSongs.length === 1) {
            playSong(filteredSongs[0]);
            return;
        }

        let randomIndex = Math.floor(Math.random() * filteredSongs.length);
        if (randomIndex === currentIndex) {
            randomIndex = (randomIndex + 1) % filteredSongs.length;
        }

        playSong(filteredSongs[randomIndex]);
    }

    async function handleExtractSubmit(event) {
        event.preventDefault();

        const sourceFile = sourceFileInput.files?.[0];
        if (!sourceFile) {
            updateExtractStatus("请先选择直播录播文件。", true);
            return;
        }

        const options = getCurrentExtractionOptions();
        const formData = new FormData();
        formData.append("file", sourceFile);
        Object.entries(options).forEach(([key, value]) => formData.append(key, String(value)));

        extractButton.disabled = true;
        updateExtractStatus("正在上传并分析录播，这一步可能需要几十秒到几分钟。", false);
        extractResults.hidden = true;
        extractResults.innerHTML = "";

        try {
            const data = await fetchJson("/api/songcuts/extract", {
                method: "POST",
                body: formData,
            });
            updateExtractStatus(
                `${data.message}。共导出 ${data.saved_count} 段，原始时长 ${formatDuration(data.analysis?.total_duration)}。`,
                false
            );
            renderExtractResults(data);
            await loadSongcuts();
            if (data.segments?.length) {
                categoryFilter.value = data.category || "";
                applyFilters();
            }
        } catch (error) {
            console.error("手动提取失败:", error);
            updateExtractStatus(error.message || "自动提取失败，请稍后重试。", true);
        } finally {
            extractButton.disabled = false;
        }
    }

    function updateExtractStatus(message, isError) {
        extractStatus.textContent = message;
        extractStatus.classList.toggle("is-error", Boolean(isError));
    }

    function renderExtractResults(data) {
        const segments = data.segments || [];
        const analysis = data.analysis || {};

        extractResults.hidden = false;

        if (!segments.length) {
            extractResults.innerHTML = `
                <div class="result-card">
                    <h4>没有找到符合条件的完整唱段</h4>
                    <p>可以尝试降低最短唱段秒数，或者适当提高允许停顿秒数。</p>
                    <p>分析阈值 RMS：${analysis.threshold_rms ?? "-"}，音频总时长：${formatDuration(analysis.total_duration)}</p>
                </div>
            `;
            return;
        }

        const items = segments.map((segment) => `
            <article class="result-item">
                <div>
                    <h4>${segment.title}</h4>
                    <p>${formatDuration(segment.start)} - ${formatDuration(segment.end)} · 时长 ${formatDuration(segment.duration)}</p>
                </div>
                <a class="chip-link secondary-link" href="${segment.path}" target="_blank" rel="noreferrer">打开文件</a>
            </article>
        `).join("");

        extractResults.innerHTML = `
            <div class="result-card">
                <h4>提取完成</h4>
                <p>保存分类：${data.category}，ffmpeg：${data.ffmpeg_path || "未显示"}。</p>
                <p>总时长 ${formatDuration(analysis.total_duration)}，分析阈值 RMS ${analysis.threshold_rms ?? "-"}。</p>
            </div>
            <div class="result-list">${items}</div>
        `;
    }

    async function saveBrecConfig() {
        saveBrecConfigButton.disabled = true;
        updateBrecStatus("正在保存 BililiveRecorder 配置...", false);

        try {
            const options = getCurrentExtractionOptions();
            const data = await fetchJson("/api/brec/config", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    api_base_url: brecApiUrlInput.value.trim(),
                    workdir: brecWorkdirInput.value.trim(),
                    api_username: brecUsernameInput.value.trim(),
                    api_password: brecPasswordInput.value,
                    webhook_secret: brecWebhookSecretInput.value.trim(),
                    auto_extract: brecAutoExtractInput.checked,
                    auto_category: brecAutoCategoryInput.value.trim() || "录播姬自动提取",
                    ffmpeg_path: options.ffmpeg_path,
                    output_format: options.output_format,
                    min_duration: options.min_duration,
                    max_silence: options.max_silence,
                    leading_padding: options.leading_padding,
                    trailing_padding: options.trailing_padding,
                    min_active_ratio: options.min_active_ratio,
                }),
            });
            updateBrecStatus(data.message || "已保存 BililiveRecorder 配置。", false);
            applyBrecSummary(data);
        } catch (error) {
            console.error("保存 BililiveRecorder 配置失败:", error);
            updateBrecStatus(error.message || "保存配置失败。", true);
        } finally {
            saveBrecConfigButton.disabled = false;
        }
    }

    async function loadBrecSummary() {
        refreshBrecButton.disabled = true;
        updateBrecStatus("正在读取 BililiveRecorder 状态...", false);

        try {
            const data = await fetchJson("/api/brec/summary");
            applyBrecSummary(data);
        } catch (error) {
            console.error("读取 BililiveRecorder 状态失败:", error);
            updateBrecStatus(error.message || "读取 BililiveRecorder 状态失败。", true);
        } finally {
            refreshBrecButton.disabled = false;
        }
    }

    function applyBrecSummary(data) {
        const config = data.config || {};
        const api = data.api || {};
        const recordings = data.recordings || [];

        brecApiUrlInput.value = config.api_base_url || "";
        brecWorkdirInput.value = config.workdir || "";
        brecUsernameInput.value = config.api_username || "";
        brecPasswordInput.value = config.api_password || "";
        brecWebhookSecretInput.value = config.webhook_secret || "";
        brecAutoExtractInput.checked = Boolean(config.auto_extract);
        brecAutoCategoryInput.value = config.auto_category || "录播姬自动提取";
        ffmpegPathInput.value = config.ffmpeg_path || ffmpegPathInput.value;
        outputFormatSelect.value = config.output_format || outputFormatSelect.value;
        minDurationInput.value = String(config.min_duration ?? minDurationInput.value);
        maxSilenceInput.value = String(config.max_silence ?? maxSilenceInput.value);
        leadingPaddingInput.value = String(config.leading_padding ?? leadingPaddingInput.value);
        trailingPaddingInput.value = String(config.trailing_padding ?? trailingPaddingInput.value);
        activeRatioInput.value = String(config.min_active_ratio ?? activeRatioInput.value);

        if (api.available) {
            updateBrecStatus(`已连接 BililiveRecorder API，房间数 ${api.room_count}。`, false);
        } else if (api.error) {
            updateBrecStatus(`录播姬 API 暂不可用：${api.error}`, true);
        } else {
            updateBrecStatus("BililiveRecorder 已配置，但还没读取到 API 状态。", false);
        }

        renderWebhookBox(data.webhook_url, config.auto_extract);
        renderRoomList(api.rooms || []);
        renderRecordingList(recordings);
    }

    function updateBrecStatus(message, isError) {
        brecStatus.textContent = message;
        brecStatus.classList.toggle("is-error", Boolean(isError));
    }

    function renderWebhookBox(webhookUrl, autoExtractEnabled) {
        brecWebhookBox.hidden = false;
        brecWebhookBox.innerHTML = `
            <div class="mini-card">
                <h4>Webhook 地址</h4>
                <p><code>${webhookUrl || "请先保存配置后生成"}</code></p>
                <p>${autoExtractEnabled ? "已开启自动提取，录播姬收到 FileClosed 后会自动切歌。" : "当前未开启自动提取，只能手动导入最近录播。"}</p>
            </div>
        `;
    }

    function renderRoomList(rooms) {
        brecRooms.hidden = false;

        if (!rooms.length) {
            brecRooms.innerHTML = `
                <div class="mini-card">
                    <h4>房间状态</h4>
                    <p>暂时没有从 BililiveRecorder API 读到房间列表。</p>
                </div>
            `;
            return;
        }

        const items = rooms.map((room) => `
            <article class="mini-item">
                <div>
                    <h4>${escapeHtml(room.name || `房间 ${room.room_id || ""}`)}</h4>
                    <p>标题：${escapeHtml(room.title || "未命名直播")}</p>
                </div>
                <div class="badge-row">
                    <span class="badge ${room.streaming ? "is-live" : ""}">${room.streaming ? "直播中" : "未开播"}</span>
                    <span class="badge ${room.recording ? "is-recording" : ""}">${room.recording ? "录制中" : "未录制"}</span>
                </div>
            </article>
        `).join("");

        brecRooms.innerHTML = `
            <div class="mini-card">
                <h4>房间状态</h4>
                <div class="mini-items">${items}</div>
            </div>
        `;
    }

    function renderRecordingList(recordings) {
        brecRecordings.hidden = false;

        if (!recordings.length) {
            brecRecordings.innerHTML = `
                <div class="mini-card">
                    <h4>最近录播</h4>
                    <p>当前工作目录下还没有找到可导入的录播文件。</p>
                </div>
            `;
            return;
        }

        const items = recordings.map((item) => `
            <article class="result-item">
                <div>
                    <h4>${escapeHtml(item.title)}</h4>
                    <p>${escapeHtml(item.relative_path)} · ${formatBytes(item.size_bytes)} · ${formatDate(item.modified_at)}</p>
                </div>
                <button type="button" class="import-brec-button" data-relative-path="${escapeAttribute(item.relative_path)}">导入歌切</button>
            </article>
        `).join("");

        brecRecordings.innerHTML = `
            <div class="mini-card">
                <h4>最近录播</h4>
                <div class="result-list">${items}</div>
            </div>
        `;

        brecRecordings.querySelectorAll(".import-brec-button").forEach((button) => {
            button.addEventListener("click", () => importBrecRecording(button.dataset.relativePath));
        });
    }

    async function importBrecRecording(relativePath) {
        updateBrecStatus(`正在导入录播：${relativePath}`, false);

        try {
            const options = getCurrentExtractionOptions();
            const data = await fetchJson("/api/brec/import", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    relative_path: relativePath,
                    category: brecAutoCategoryInput.value.trim() || options.category,
                    output_format: options.output_format,
                    min_duration: options.min_duration,
                    max_silence: options.max_silence,
                    leading_padding: options.leading_padding,
                    trailing_padding: options.trailing_padding,
                    min_active_ratio: options.min_active_ratio,
                    ffmpeg_path: options.ffmpeg_path,
                }),
            });

            updateBrecStatus(`${data.message}：${relativePath}`, false);
            renderExtractResults(data);
            await loadSongcuts();
            await loadBrecSummary();
            if (data.segments?.length) {
                categoryFilter.value = data.category || "";
                applyFilters();
            }
        } catch (error) {
            console.error("导入录播失败:", error);
            updateBrecStatus(error.message || "导入录播失败。", true);
        }
    }

    function formatDuration(value) {
        if (typeof value !== "number" || Number.isNaN(value)) {
            return "--:--";
        }

        const totalSeconds = Math.max(0, Math.round(value));
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;

        if (hours > 0) {
            return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
        }

        return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }

    function formatBytes(value) {
        if (typeof value !== "number" || Number.isNaN(value)) {
            return "-";
        }

        const units = ["B", "KB", "MB", "GB"];
        let size = value;
        let unitIndex = 0;
        while (size >= 1024 && unitIndex < units.length - 1) {
            size /= 1024;
            unitIndex += 1;
        }
        return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
    }

    function formatDate(value) {
        if (!value) {
            return "-";
        }

        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return value;
        }

        return date.toLocaleString("zh-CN", { hour12: false });
    }

    function escapeHtml(value) {
        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    function escapeAttribute(value) {
        return escapeHtml(value);
    }
});
