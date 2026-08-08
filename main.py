import os
import uuid
import shutil
import asyncio
import datetime
import mimetypes
import hashlib
import hmac
import json
import logging
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from google.cloud import storage
from google.oauth2 import service_account
from pydantic_settings import BaseSettings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """Application settings from environment variables."""
    gcs_bucket_name: str = ""
    gcp_sa_key_path: str = ""
    ig_user_id: str = ""
    ig_access_token: str = ""
    ig_app_secret: str = ""
    webhook_verify_token: str = "default_verify_token"
    webhook_url: str = ""
    max_upload_size_mb: int = 50  # Maximum file size in MB
    job_timeout_minutes: int = 10
    polling_interval_seconds: int = 60
    polling_max_attempts: int = 10
    
    class Config:
        env_file = ".env"

settings = Settings()

app = FastAPI(title="IG Publisher Web")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store with async lock
# SRE Note: Replace with Redis/Valkey for multi-replica K8s deployments
jobs = {}
jobs_lock = asyncio.Lock()

# Draft store for unpublished media containers
# SRE Note: Replace with persistent storage (Redis/Database) for production
drafts = {}
drafts_lock = asyncio.Lock()

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

async def process_ig_publish(job_id: str, file_path: str, caption: str, mime_type: str, is_draft: bool = False):
    """Background task that handles the entire GCS -> IG API lifecycle."""
    async with jobs_lock:
        jobs[job_id]["status"] = "UPLOADING_TO_GCS"
        jobs[job_id]["log"] = "Uploading media to secure storage..."
    
    try:
        bucket_name = settings.gcs_bucket_name
        key_path = settings.gcp_sa_key_path
        ig_user_id = settings.ig_user_id
        access_token = settings.ig_access_token
        
        if not all([bucket_name, key_path, ig_user_id, access_token]):
            raise ValueError("Missing required environment variables.")

        public_url = await upload_to_gcs(file_path, bucket_name, key_path)
        
        async with jobs_lock:
            jobs[job_id]["status"] = "CREATING_CONTAINER"
            jobs[job_id]["log"] = "Creating Meta media container..."
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {"caption": caption}
            if mime_type.startswith('image/'):
                payload["image_url"] = public_url
                payload["media_type"] = "IMAGE"
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
            
            # If creating a draft, store it and stop here
            if is_draft:
                async with jobs_lock:
                    jobs[job_id]["status"] = "DRAFT_CREATED"
                    jobs[job_id]["log"] = f"Draft created successfully. Container ID: {container_id}"
                    jobs[job_id]["container_id"] = container_id
                
                # Store draft metadata
                async with drafts_lock:
                    drafts[container_id] = {
                        "job_id": job_id,
                        "caption": caption,
                        "mime_type": mime_type,
                        "created_at": datetime.datetime.utcnow().isoformat(),
                        "status": "draft"
                    }
                
                logger.info(f"Draft created with container_id: {container_id}")
                
                # Clean up local file
                if os.path.exists(file_path):
                    os.remove(file_path)
                return
            
            async with jobs_lock:
                jobs[job_id]["status"] = "PROCESSING_META"
                jobs[job_id]["log"] = f"Waiting for Meta to process (this can take a few minutes for video)..."
            
            # Polling (configurable max attempts and interval)
            for attempt in range(settings.polling_max_attempts): 
                await asyncio.sleep(settings.polling_interval_seconds)
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
                
                async with jobs_lock:
                    jobs[job_id]["log"] = f"Meta status: {status}. Attempt {attempt + 1}/{settings.polling_max_attempts}..."
                logger.info(f"Container {container_id} status: {status} (attempt {attempt + 1})")
            else:
                raise Exception("Timeout waiting for Meta processing.")
                
            async with jobs_lock:
                jobs[job_id]["status"] = "PUBLISHING"
                jobs[job_id]["log"] = "Publishing to Instagram..."
            
            pub_res = await client.post(
                f"{GRAPH_API_BASE}/{ig_user_id}/media_publish", 
                json={"creation_id": container_id}, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            pub_res.raise_for_status()
            media_id = pub_res.json().get("id")
            
            async with jobs_lock:
                jobs[job_id]["status"] = "SUCCESS"
                jobs[job_id]["log"] = f"Successfully published! Media ID: {media_id}"
                jobs[job_id]["media_id"] = media_id
            
            logger.info(f"Published successfully! Media ID: {media_id}")
            
    except Exception as e:
        logger.error(f"Job {job_id} failed: {str(e)}")
        async with jobs_lock:
            jobs[job_id]["status"] = "FAILED"
            jobs[job_id]["log"] = f"Error: {str(e)}"
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@app.post("/publish")
async def publish(background_tasks: BackgroundTasks, file: UploadFile = File(...), caption: str = Form(""), is_draft: bool = Form(False)):
    mime_type, _ = mimetypes.guess_type(file.filename)
    if not mime_type or not (mime_type.startswith('image/') or mime_type.startswith('video/')):
        raise HTTPException(status_code=400, detail="Invalid file type. Only images and videos are allowed.")

    # Validate file size
    file_size = 0
    content = await file.read()
    file_size = len(content)
    max_size_bytes = settings.max_upload_size_mb * 1024 * 1024
    
    if file_size > max_size_bytes:
        raise HTTPException(
            status_code=400, 
            detail=f"File too large. Maximum size is {settings.max_upload_size_mb}MB."
        )
    
    # Reset file pointer after reading
    file.file.seek(0)

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    async with jobs_lock:
        jobs[job_id] = {"status": "QUEUED", "log": "Job received."}
    
    logger.info(f"Job {job_id} created for file {file.filename}, draft={is_draft}")
    background_tasks.add_task(process_ig_publish, job_id, file_path, caption, mime_type, is_draft)
    
    return {"job_id": job_id, "is_draft": is_draft}


@app.post("/drafts/{container_id}/publish")
async def publish_draft(background_tasks: BackgroundTasks, container_id: str):
    """Publish an existing draft by its container ID."""
    async with drafts_lock:
        if container_id not in drafts:
            raise HTTPException(status_code=404, detail="Draft not found")
        
        # Mark draft as being published
        drafts[container_id]["status"] = "publishing"
    
    job_id = str(uuid.uuid4())
    
    # Create a new job for publishing the draft
    async with jobs_lock:
        jobs[job_id] = {
            "status": "QUEUED", 
            "log": "Publishing draft...",
            "container_id": container_id
        }
    
    logger.info(f"Job {job_id} created to publish draft {container_id}")
    
    # Start background task to publish the draft
    background_tasks.add_task(publish_draft_container, job_id, container_id)
    
    return {"job_id": job_id, "container_id": container_id}


async def publish_draft_container(job_id: str, container_id: str):
    """Background task to publish an existing draft container."""
    try:
        ig_user_id = settings.ig_user_id
        access_token = settings.ig_access_token
        
        if not all([ig_user_id, access_token]):
            raise ValueError("Missing required environment variables.")
        
        async with jobs_lock:
            jobs[job_id]["status"] = "CHECKING_CONTAINER"
            jobs[job_id]["log"] = "Checking container status..."
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Check container status first
            status_res = await client.get(
                f"{GRAPH_API_BASE}/{container_id}", 
                params={"fields": "status_code"}, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            status_res.raise_for_status()
            status = status_res.json().get("status_code")
            
            if status == "ERROR" or status == "EXPIRED":
                raise Exception(f"Container is in invalid state: {status}")
            
            # If still processing, wait for it
            if status != "FINISHED":
                async with jobs_lock:
                    jobs[job_id]["status"] = "PROCESSING_META"
                    jobs[job_id]["log"] = "Waiting for Meta to process..."
                
                for attempt in range(settings.polling_max_attempts):
                    await asyncio.sleep(settings.polling_interval_seconds)
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
                    
                    logger.info(f"Draft container {container_id} status: {status} (attempt {attempt + 1})")
                else:
                    raise Exception("Timeout waiting for Meta processing.")
            
            async with jobs_lock:
                jobs[job_id]["status"] = "PUBLISHING"
                jobs[job_id]["log"] = "Publishing to Instagram..."
            
            pub_res = await client.post(
                f"{GRAPH_API_BASE}/{ig_user_id}/media_publish", 
                json={"creation_id": container_id}, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            pub_res.raise_for_status()
            media_id = pub_res.json().get("id")
            
            async with jobs_lock:
                jobs[job_id]["status"] = "SUCCESS"
                jobs[job_id]["log"] = f"Successfully published! Media ID: {media_id}"
                jobs[job_id]["media_id"] = media_id
            
            # Update draft status
            async with drafts_lock:
                drafts[container_id]["status"] = "published"
                drafts[container_id]["media_id"] = media_id
                drafts[container_id]["published_at"] = datetime.datetime.utcnow().isoformat()
            
            logger.info(f"Draft {container_id} published successfully! Media ID: {media_id}")
            
    except Exception as e:
        logger.error(f"Failed to publish draft {container_id}: {str(e)}")
        async with jobs_lock:
            jobs[job_id]["status"] = "FAILED"
            jobs[job_id]["log"] = f"Error: {str(e)}"
        async with drafts_lock:
            if container_id in drafts:
                drafts[container_id]["status"] = "publish_failed"


@app.get("/drafts")
async def list_drafts():
    """List all stored drafts."""
    async with drafts_lock:
        return {"drafts": dict(drafts)}


@app.get("/drafts/{container_id}")
async def get_draft(container_id: str):
    """Get details of a specific draft."""
    async with drafts_lock:
        if container_id not in drafts:
            raise HTTPException(status_code=404, detail="Draft not found")
        return dict(drafts[container_id])


@app.delete("/drafts/{container_id}")
async def delete_draft(container_id: str):
    """Delete a draft."""
    async with drafts_lock:
        if container_id not in drafts:
            raise HTTPException(status_code=404, detail="Draft not found")
        
        del drafts[container_id]
    
    logger.info(f"Draft {container_id} deleted")
    return {"message": f"Draft {container_id} deleted successfully"}


# Webhook verification and handling


@app.get("/webhooks/instagram")
async def verify_webhook(request: Request):
    """Verify webhook subscription from Instagram/Meta."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    if mode == "subscribe" and token == settings.webhook_verify_token:
        return int(challenge) if challenge.isdigit() else challenge
    
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhooks/instagram")
async def handle_webhook(request: Request):
    """Handle incoming webhook events from Instagram/Meta."""
    try:
        # Verify signature if app secret is configured
        # Cache body for signature verification and JSON parsing
        body = await request.body()
        
        if settings.ig_app_secret:
            x_hub_signature = request.headers.get("X-Hub-Signature-256", "")
            
            if x_hub_signature:
                expected_signature = hmac.new(
                    settings.ig_app_secret.encode(),
                    body,
                    hashlib.sha256
                ).hexdigest()
                
                provided_signature = x_hub_signature.replace("sha256=", "")
                
                if not hmac.compare_digest(expected_signature, provided_signature):
                    logger.warning("Invalid webhook signature received")
                    raise HTTPException(status_code=403, detail="Invalid signature")
        
        payload = json.loads(body)
        
        # Process webhook entry
        for entry in payload.get("entry", []):
            # Instagram Business Account webhook
            if entry.get("id") and entry.get("messaging"):
                for messaging_event in entry["messaging"]:
                    await process_messaging_webhook(messaging_event)
            
            # Media processing status changes
            if entry.get("changes"):
                for change in entry["changes"]:
                    await process_media_change(change)
        
        return {"status": "success"}
        
    except HTTPException:
        raise
    except Exception as e:
        # Log error but return a generic message to avoid exposing internals
        logger.error(f"Webhook processing error: {str(e)}")
        return {"status": "error", "message": "An internal error occurred"}


async def process_messaging_webhook(event: dict):
    """Process messaging webhook events (comments, mentions, etc.)."""
    sender_id = event.get("sender", {}).get("id")
    recipient_id = event.get("recipient", {}).get("id")
    timestamp = event.get("timestamp")
    
    # Handle different message types
    if "message" in event:
        message = event["message"]
        # Could handle direct messages here if needed
        pass
    
    if "comment" in event:
        comment = event["comment"]
        # Store or process comment notification
        print(f"New comment from {sender_id}: {comment}")


async def process_media_change(change: dict):
    """Process media status change notifications."""
    value = change.get("value", {})
    field = change.get("field", "")
    
    if field == "media_status":
        container_id = value.get("media_id")
        status = value.get("status")
        
        # Update job status if we're tracking this container
        async with jobs_lock:
            for job_id, job in jobs.items():
                if job.get("container_id") == container_id:
                    job["status"] = f"META_{status}"
                    job["log"] = f"Meta status updated via webhook: {status}"
                    logger.info(f"Job {job_id} status updated via webhook: {status}")
                    break
        
        # Update draft status if applicable
        async with drafts_lock:
            if container_id in drafts:
                drafts[container_id]["meta_status"] = status
                drafts[container_id]["status_updated_at"] = datetime.datetime.utcnow().isoformat()


@app.get("/webhooks/config")
async def get_webhook_config():
    """Get current webhook configuration info."""
    return {
        "webhook_url": settings.webhook_url,
        "verify_token_configured": bool(settings.webhook_verify_token),
        "app_secret_configured": bool(settings.ig_app_secret),
        "setup_instructions": [
            "1. Go to Facebook Developer Dashboard",
            "2. Select your app and navigate to Instagram Graph API",
            "3. Add a webhook subscription for 'instagram' object",
            "4. Set callback URL to: https://your-domain.com/webhooks/instagram",
            "5. Use the WEBHOOK_VERIFY_TOKEN environment variable as verify token",
            "6. Subscribe to fields: media_status, comments, mentions"
        ]
    }


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Get status of a specific job by ID."""
    async with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(jobs[job_id])


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """Get status of a specific job by ID (alias for /jobs/{job_id})."""
    async with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(jobs[job_id])


@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration."""
    return {"status": "healthy", "timestamp": datetime.datetime.utcnow().isoformat()}

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read())
