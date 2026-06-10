import os
import uuid
import shutil
import asyncio
import datetime
import mimetypes
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
import httpx
from google.cloud import storage
from google.oauth2 import service_account

app = FastAPI(title="IG Publisher Web")

# In-memory job store 
# SRE Note: Replace with Redis/Valkey for multi-replica K8s deployments
jobs = {}

GRAPH_API_VERSION = "v24.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
UPLOAD_DIR = "/tmp/ig-uploads"

os.makedirs(UPLOAD_DIR, exist_ok=True)

async def upload_to_gcs(file_path: str, bucket_name: str, key_path: str) -> str:
    """Runs the blocking GCS SDK in a thread pool to avoid blocking the async event loop."""
    def _sync_upload():
        credentials = service_account.Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
        )
        client = storage.Client(credentials=credentials, project=credentials.project_id)
        bucket = client.bucket(bucket_name)
        blob_name = f"ig-uploads/{uuid.uuid4()}-{os.path.basename(file_path)}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(file_path)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=60),
            method="GET",
        )
    
    return await asyncio.to_thread(_sync_upload)

async def process_ig_publish(job_id: str, file_path: str, caption: str, mime_type: str):
    """Background task that handles the entire GCS -> IG API lifecycle."""
    jobs[job_id]["status"] = "UPLOADING_TO_GCS"
    jobs[job_id]["log"] = "Uploading media to secure storage..."
    try:
        bucket_name = os.environ.get("GCS_BUCKET_NAME")
        key_path = os.environ.get("GCP_SA_KEY_PATH")
        ig_user_id = os.environ.get("IG_USER_ID")
        access_token = os.environ.get("IG_ACCESS_TOKEN")
        
        if not all([bucket_name, key_path, ig_user_id, access_token]):
            raise ValueError("Missing required environment variables.")

        public_url = await upload_to_gcs(file_path, bucket_name, key_path)
        
        jobs[job_id]["status"] = "CREATING_CONTAINER"
        jobs[job_id]["log"] = "Creating Meta media container..."
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {"caption": caption}
            if mime_type.startswith('image/'):
                payload["image_url"] = public_url
            elif mime_type.startswith('video/'):
                payload["media_type"] = "REELS"
                payload["video_url"] = public_url
            else:
                raise ValueError("Unsupported file type.")
                
            res = await client.post(
                f"{GRAPH_API_BASE}/{ig_user_id}/media", 
                json=payload, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            res.raise_for_status()
            container_id = res.json().get("id")
            
            jobs[job_id]["status"] = "PROCESSING_META"
            jobs[job_id]["log"] = "Waiting for Meta to process (this can take a few minutes for video)..."
            
            # Polling (max 10 minutes)
            for _ in range(10): 
                await asyncio.sleep(60)
                status_res = await client.get(
                    f"{GRAPH_API_BASE}/{container_id}", 
                    params={"fields": "status_code"}, 
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                status_res.raise_for_status()
                status = status_res.json().get("status_code")
                
                if status == "FINISHED":
                    break
                elif status in ["ERROR", "EXPIRED"]:
                    raise Exception(f"Meta processing failed with status: {status}")
                
                jobs[job_id]["log"] = f"Meta status: {status}. Retrying in 60s..."
            else:
                raise Exception("Timeout waiting for Meta processing.")
                
            jobs[job_id]["status"] = "PUBLISHING"
            jobs[job_id]["log"] = "Publishing to Instagram..."
            pub_res = await client.post(
                f"{GRAPH_API_BASE}/{ig_user_id}/media_publish", 
                json={"creation_id": container_id}, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            pub_res.raise_for_status()
            media_id = pub_res.json().get("id")
            
            jobs[job_id]["status"] = "SUCCESS"
            jobs[job_id]["log"] = f"Successfully published! Media ID: {media_id}"
            jobs[job_id]["media_id"] = media_id
            
    except Exception as e:
        jobs[job_id]["status"] = "FAILED"
        jobs[job_id]["log"] = f"Error: {str(e)}"
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@app.post("/publish")
async def publish(background_tasks: BackgroundTasks, file: UploadFile = File(...), caption: str = Form("")):
    mime_type, _ = mimetypes.guess_type(file.filename)
    if not mime_type or not (mime_type.startswith('image/') or mime_type.startswith('video/')):
        raise HTTPException(status_code=400, detail="Invalid file type. Only images and videos are allowed.")

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    jobs[job_id] = {"status": "QUEUED", "log": "Job received."}
    background_tasks.add_task(process_ig_publish, job_id, file_path, caption, mime_type)
    
    return {"job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read())