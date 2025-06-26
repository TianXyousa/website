document.addEventListener('DOMContentLoaded', function() {
    // 获取DOM元素
    const uploadForm = document.getElementById('uploadForm');
    const audioFileInput = document.getElementById('audioFile');
    const categorySelect = document.getElementById('category');
    const passwordInput = document.getElementById('password');
    const uploadStatus = document.getElementById('uploadStatus');
    const audioList = document.getElementById('audioList');
    const filterCategory = document.getElementById('filterCategory');
    
    // 加载分类列表
    loadCategories();
    
    // 加载音频列表
    loadAudioList();
    
    // 分类选择变更事件
    filterCategory.addEventListener('change', function() {
        loadAudioList(this.value);
    });
    
    // 上传表单提交事件
    uploadForm.addEventListener('submit', function(e) {
        e.preventDefault();
        
        const file = audioFileInput.files[0];
        if (!file) {
            showStatus('请选择一个音频文件', 'error');
            return;
        }
        
        const formData = new FormData();
        formData.append('file', file);
        formData.append('category', categorySelect.value);
        
        const password = passwordInput.value;
        if (!password) {
            showStatus('请输入上传密码', 'error');
            return;
        }
        
        uploadAudio(formData, password);
    });
    
    // 加载分类列表函数
    async function loadCategories() {
        try {
            const response = await fetch('/api/categories');
            const data = await response.json();
            
            // 填充分类选择框
            categorySelect.innerHTML = '';
            filterCategory.innerHTML = '<option value="">全部</option>';
            
            data.categories.forEach(category => {
                // 上传表单的分类选择
                const option = document.createElement('option');
                option.value = category;
                option.textContent = category;
                categorySelect.appendChild(option);
                
                // 筛选的分类选择
                const filterOption = document.createElement('option');
                filterOption.value = category;
                filterOption.textContent = category;
                filterCategory.appendChild(filterOption);
            });
        } catch (error) {
            console.error('加载分类列表失败:', error);
        }
    }
    
    // 上传音频函数
    async function uploadAudio(formData, password) {
        try {
            showStatus('上传中...', '');
            
            const response = await fetch('/api/upload-audio', {
                method: 'POST',
                headers: {
                    'X-API-Key': password
                },
                body: formData
            });
            
            const data = await response.json();
            
            if (response.ok) {
                showStatus(`文件 ${data.filename} 上传成功！`, 'success');
                uploadForm.reset();
                loadAudioList(); // 重新加载音频列表
            } else {
                showStatus(`上传失败: ${data.message || '密码错误'}`, 'error');
            }
        } catch (error) {
            showStatus(`上传错误: ${error.message}`, 'error');
        }
    }
    
    // 加载音频列表函数
    async function loadAudioList(category = null) {
        try {
            let url = '/api/audio-list';
            if (category) {
                url += `?category=${encodeURIComponent(category)}`;
            }
            
            const response = await fetch(url);
            const data = await response.json();
            
            // 清空列表
            audioList.innerHTML = '';
            
            // 检查是否有数据
            const categories = data.categories;
            let hasFiles = false;
            
            // 遍历每个分类
            for (const [categoryName, files] of Object.entries(categories)) {
                if (files.length > 0) {
                    hasFiles = true;
                    
                    // 创建分类区域
                    const categorySection = document.createElement('div');
                    categorySection.className = 'category-section';
                    
                    // 分类标题
                    const categoryTitle = document.createElement('div');
                    categoryTitle.className = 'category-title';
                    categoryTitle.textContent = categoryName;
                    categorySection.appendChild(categoryTitle);
                    
                    // 创建音频网格
                    const audioGrid = document.createElement('div');
                    audioGrid.className = 'audio-grid';
                    
                    // 添加音频项
                    files.forEach(file => {
                        // 创建播放按钮作为音频项
                        const playButton = document.createElement('button');
                        playButton.className = 'play-button';
                        
                        const fileNameWithoutExt = file.filename.replace(/\.[^/.]+$/, '');
                        // 显示文件名在按钮上
                        playButton.textContent = fileNameWithoutExt;
                        playButton.dataset.audioSrc = file.path;
                        
                        // 添加点击事件
                        playButton.addEventListener('click', function() {
                            playAudio(this.dataset.audioSrc);
                        });
                        
                        // 直接添加按钮到网格中
                        audioGrid.appendChild(playButton);
                    });
                    
                    categorySection.appendChild(audioGrid);
                    audioList.appendChild(categorySection);
                }
            }
            
            if (!hasFiles) {
                audioList.innerHTML = '<p>没有找到音频文件</p>';
            }
        } catch (error) {
            console.error('加载音频列表失败:', error);
            audioList.innerHTML = '<p>加载音频列表失败</p>';
        }
    }
    // 添加到script.js文件末尾
    // 音频播放函数
    function playAudio(audioSrc) {
        // 如果有正在播放的音频，先停止它
        if (window.currentAudio) {
            window.currentAudio.pause();
            window.currentAudio.currentTime = 0;
        }
        
        // 创建新的音频对象并播放
        const audio = new Audio(audioSrc);
        audio.play();
        window.currentAudio = audio;
    }

    // 状态显示函数
    function showStatus(message, type = 'info') {
        // 获取状态显示元素
        const statusElement = document.getElementById('status-container');
        
        if (!statusElement) {
            console.error('状态显示元素不存在');
            return;
        }
        
        // 清除之前的状态类
        statusElement.className = 'status';
        
        // 添加新的状态类型
        statusElement.classList.add(`status-${type}`);
        
        // 设置消息内容
        statusElement.textContent = message;
        
        // 显示状态元素
        statusElement.style.display = 'block';
        
        // 5秒后自动隐藏
        setTimeout(() => {
            statusElement.style.display = 'none';
        }, 5000);
    }
});