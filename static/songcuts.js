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
    loadSongcuts();

    categoryFilter.addEventListener("change", applyFilters);
    searchInput.addEventListener("input", applyFilters);
    prevButton.addEventListener("click", () => playRelative(-1));
    nextButton.addEventListener("click", () => playRelative(1));
    toggleButton.addEventListener("click", togglePlayback);
    randomButton.addEventListener("click", playRandomSong);
    playbackMode.addEventListener("change", handlePlaybackModeChange);
    player.addEventListener("ended", handlePlaybackEnded);

    function initializePlaybackMode() {
        const storedMode = window.localStorage.getItem(PLAYBACK_MODE_STORAGE_KEY);
        const initialMode = playbackModeText[storedMode] ? storedMode : "pause";
        playbackMode.value = initialMode;
        updateModeHint(initialMode);
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

        const mode = playbackMode.value;
        if (mode === "next") {
            playRelative(1);
            return;
        }

        if (mode === "random") {
            playRandomSong();
        }
    }

    async function loadSongcuts() {
        resultSummary.textContent = "正在加载歌切...";

        try {
            const response = await fetch("/api/songcuts");
            const data = await response.json();
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
        const categories = [...new Set(songs.map((song) => song.category))].sort((left, right) => left.localeCompare(right, "zh-CN"));
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
            const matchKeyword = !keyword || haystack.includes(keyword);
            return matchCategory && matchKeyword;
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
            currentMeta.textContent = "浏览器阻止了自动播放，请再点一次播放按钮";
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
            player.play().catch((error) => {
                console.error("恢复播放失败:", error);
            });
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
});
