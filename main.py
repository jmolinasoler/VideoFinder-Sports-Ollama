# main.py
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse
import shutil
import os
import cv2
import ollama
import time
from pathlib import Path
import asyncio
import json

app = FastAPI()

# 创建必要的目录
UPLOAD_DIR = Path("uploads")
FRAMES_DIR = Path("frames")
UPLOAD_DIR.mkdir(exist_ok=True)
FRAMES_DIR.mkdir(exist_ok=True)

# 设置模板和上传/帧目录的静态文件服务
templates = Jinja2Templates(directory="templates")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/frames", StaticFiles(directory="frames"), name="frames")


async def analyze_image(image_path: str, object_str: str):
    """异步版本的图像分析函数"""
    prompt_str = f"""Please analyze the image like a sports {object_str} scout and answer the following questions:
    1. Which {object_str} action is taking place in the image?
    2. If yes, describe which team is doing the action.
    3. If no, describe the position of the players in the court.
    4. On a scale of 1-10, how confident are you in your answer?

    Please structure your response as follows:
    Answer: [YES/NO]
    Description: [Your detailed description]
    Confidence: [1-10]"""

    try:
        response = await asyncio.to_thread(
            ollama.chat,
            model='llama3.2-vision',
            messages=[{
                'role': 'user',
                'content': prompt_str,
                'images': [image_path]
            }]
        )

        response_text = response['message']['content']
        response_lines = response_text.strip().split('\n')

        answer = None
        description = None
        confidence = 10

        for line in response_lines:
            line = line.strip()
            if line.lower().startswith('answer:'):
                answer = line.split(':', 1)[1].strip().upper()
            elif any(line.lower().startswith(prefix) for prefix in
                     ['description:', 'reasoning:', 'alternative description:']):
                description = line.split(':', 1)[1].strip()
            elif line.lower().startswith('confidence:'):
                try:
                    confidence = int(line.split(':', 1)[1].strip())
                except ValueError:
                    confidence = 10

        return answer == "YES" and confidence >= 7, description, confidence
    except Exception as e:
        print(f"Error during image analysis: {str(e)}")
        return False, "Error occurred", 0


def preprocess_image(image_path):
    """图像预处理函数"""
    img = cv2.imread(image_path)
    if img is None:
        return False

    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    limg = cv2.merge((cl, a, b))
    final = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)
    cv2.imwrite(image_path, final, [cv2.IMWRITE_JPEG_QUALITY, 100])
    return True


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/analyze")
async def analyze_video(
        video: UploadFile = File(...),
        object_str: str = Form(...)
):
    try:
        # 保存上传的视频
        video_path = UPLOAD_DIR / video.filename
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)

        # 为当前任务创建专门的帧目录
        task_frames_dir = FRAMES_DIR / video.filename.split('.')[0]
        task_frames_dir.mkdir(exist_ok=True)

        # 异步生成分析结果
        async def generate_results():
            cap = cv2.VideoCapture(str(video_path))
            fps = int(cap.get(cv2.CAP_PROP_FPS))
            frame_count = 0

            try:
                while True:
                    success, frame = cap.read()
                    if not success:
                        break

                    if frame_count % fps == 0:  # 每秒处理一帧
                        current_second = frame_count // fps
                        frame_path = os.path.join(task_frames_dir, f"frame_{current_second}.jpg")
                        cv2.imwrite(frame_path, frame)

                        if preprocess_image(frame_path):
                            is_match, description, confidence = await analyze_image(frame_path, object_str)

                            result = {
                                "status": "success",
                                "frame": {
                                    "second": current_second,
                                    "is_match": is_match,
                                    "description": description,
                                    "confidence": confidence,
                                    "frame_path": f"/frames/{video.filename.split('.')[0]}/frame_{current_second}.jpg"
                                }
                            }

                            yield json.dumps(result) + "\n"

                    frame_count += 1

            finally:
                cap.release()

        return StreamingResponse(generate_results(), media_type="application/json")

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)