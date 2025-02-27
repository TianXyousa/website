from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Security
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
import os
import shutil
from pathlib import Path
from typing import List, Optional

app = FastAPI()

# 预设上传密码 (实际应用中应使用加密哈希存储)
UPLOAD_PASSWORD = "your_secure_password"

# API密钥验证
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# 密码验证函数
def verify_password(api_key: str = Security(api_key_header)):
    if api_key != UPLOAD_PASSWORD:
        raise HTTPException(
            status_code=401, 
            detail="无效密码"
        )
    return api_key

# 确保音频目录存在
AUDIO_DIR = Path("assets/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# 预先创建分类文件夹
CATEGORIES = ["小蛙招呼", "小蛙怪叫", "小蛙怪话", "蛙言蛙语", "认同", "道歉", "疑问", "感谢", "高兴", "遗憾", "笨蛋", "生气", "盐蛙", "蛙笑", "删！"]
for category in CATEGORIES:
    os.makedirs(AUDIO_DIR / category, exist_ok=True)

# 上传音频文件API (带密码保护)
@app.post("/api/upload-audio")
async def upload_audio(
    file: UploadFile = File(...),
    category: str = Form("其他"),  # 默认分类为"其他"
    password: str = Depends(verify_password)  # 依赖注入密码验证
):
    try:
        # 验证分类是否存在
        if category not in CATEGORIES:
            return JSONResponse(
                status_code=400,
                content={"message": f"无效分类: {category}. 有效分类: {', '.join(CATEGORIES)}"}
            )
            
        # 保存文件到对应分类文件夹
        category_dir = AUDIO_DIR / category
        file_location = category_dir / file.filename
        
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        return {"filename": file.filename, "category": category, "status": "success"}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"message": f"上传失败: {str(e)}"}
        )

# 获取音频文件列表API (带分类)
@app.get("/api/audio-list")
async def get_audio_list(category: Optional[str] = None):
    result = {}
    
    # 如果指定了分类，只返回该分类的文件
    if category:
        if category not in CATEGORIES:
            return JSONResponse(
                status_code=400,
                content={"message": f"无效分类: {category}"}
            )
        categories_to_scan = [category]
    else:
        # 否则返回所有分类
        categories_to_scan = CATEGORIES
    
    # 按分类收集文件
    for cat in categories_to_scan:
        cat_dir = AUDIO_DIR / cat
        if os.path.exists(cat_dir):
            files = []
            for file in os.listdir(cat_dir):
                if file.endswith(('.mp3', '.wav', '.ogg')):
                    files.append({
                        "filename": file,
                        "path": f"/assets/audio/{cat}/{file}",
                        "category": cat
                    })
            result[cat] = files
    
    return {"categories": result}

# 获取所有分类列表
@app.get("/api/categories")
async def get_categories():
    return {"categories": CATEGORIES}

# 静态文件服务
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
app.mount("/", StaticFiles(directory="static", html=True), name="static")