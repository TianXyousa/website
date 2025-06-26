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
            // 创建进度条UI
            uploadStatus.innerHTML = `
                <div class="progress-text">准备上传...</div>
                <div class="progress-container">
                    <div class="progress-bar"></div>
                </div>
            `;
            
            // 获取进度条元素
            const progressBar = uploadStatus.querySelector('.progress-bar');
            const progressText = uploadStatus.querySelector('.progress-text');
            
            // 使用XMLHttpRequest来获取进度
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                
                xhr.upload.addEventListener('progress', (event) => {
                    if (event.lengthComputable) {
                        const progress = Math.round((event.loaded / event.total) * 100);
                        // 更新进度条
                        progressBar.style.width = `${progress}%`;
                        // 更新文字
                        progressText.textContent = `上传中: ${progress}%`;
                    }
                });
                
                xhr.addEventListener('load', () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            const data = JSON.parse(xhr.responseText);
                            progressText.textContent = `文件 ${data.filename} 上传成功！`;
                            progressBar.style.width = '100%';
                            progressBar.style.backgroundColor = '#4caf50';
                            
                            uploadForm.reset();
                            setTimeout(() => {
                                loadAudioList(); // 重新加载音频列表
                            }, 1000);
                            
                            resolve(data);
                        } catch (e) {
                            progressText.textContent = '上传成功，但解析响应失败';
                            reject(e);
                        }
                    } else {
                        let errorMsg = '上传失败';
                        try {
                            const data = JSON.parse(xhr.responseText);
                            errorMsg = data.message || '密码错误';
                        } catch (e) {}
                        
                        progressText.textContent = `上传失败: ${errorMsg}`;
                        progressBar.style.backgroundColor = '#f44336';
                        reject(new Error(errorMsg));
                    }
                });
                
                xhr.addEventListener('error', () => {
                    progressText.textContent = '网络错误，上传失败';
                    progressBar.style.backgroundColor = '#f44336';
                    reject(new Error('Network error'));
                });
                
                xhr.open('POST', '/api/upload-audio');
                xhr.setRequestHeader('X-API-Key', password);
                xhr.send(formData);
            });
        } catch (error) {
            uploadStatus.innerHTML = `<div class="progress-text">上传错误: ${error.message}</div>`;
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