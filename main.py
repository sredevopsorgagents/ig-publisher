import os
import uuid
import shutil
import asyncio
import datetime
import mimetypes
import hashlib
import hmac
import json
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
from google.cloud import storage
from google.oauth2 import service_account

app = FastAPI(title="IG Publisher Web")

# In-memory job store 
# SRE Note: Replace with Redis/Valkey for multi-replica K8s deployments
jobs = {}

# Draft store for unpublished media containers
# SRE Note: Replace with persistent storage (Redis/Database) for production
drafts = {}

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
            
            # If creating a draft, store it and stop here
            if is_draft:
                jobs[job_id]["status"] = "DRAFT_CREATED"
                jobs[job_id]["log"] = f"Draft created successfully. Container ID: {container_id}"
                jobs[job_id]["container_id"] = container_id
                
                # Store draft metadata
                drafts[container_id] = {
                    "job_id": job_id,
                    "caption": caption,
                    "mime_type": mime_type,
                    "created_at": datetime.datetime.utcnow().isoformat(),
                    "status": "draft"
                }
                
                # Clean up local file
                if os.path.exists(file_path):
                    os.remove(file_path)
                return
            
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
async def publish(background_tasks: BackgroundTasks, file: UploadFile = File(...), caption: str = Form(""), is_draft: bool = Form(False)):
    mime_type, _ = mimetypes.guess_type(file.filename)
    if not mime_type or not (mime_type.startswith('image/') or mime_type.startswith('video/')):
        raise HTTPException(status_code=400, detail="Invalid file type. Only images and videos are allowed.")

    job_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    jobs[job_id] = {"status": "QUEUED", "log": "Job received."}
    background_tasks.add_task(process_ig_publish, job_id, file_path, caption, mime_type, is_draft)
    
    return {"job_id": job_id, "is_draft": is_draft}


@app.post("/drafts/{container_id}/publish")
async def publish_draft(background_tasks: BackgroundTasks, container_id: str):
    """Publish an existing draft by its container ID."""
    if container_id not in drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    draft = drafts[container_id]
    job_id = str(uuid.uuid4())
    
    # Create a new job for publishing the draft
    jobs[job_id] = {
        "status": "QUEUED", 
        "log": "Publishing draft...",
        "container_id": container_id
    }
    
    # Mark draft as being published
    drafts[container_id]["status"] = "publishing"
    
    # Start background task to publish the draft
    background_tasks.add_task(publish_draft_container, job_id, container_id)
    
    return {"job_id": job_id, "container_id": container_id}


async def publish_draft_container(job_id: str, container_id: str):
    """Background task to publish an existing draft container."""
    try:
        ig_user_id = os.environ.get("IG_USER_ID")
        access_token = os.environ.get("IG_ACCESS_TOKEN")
        
        if not all([ig_user_id, access_token]):
            raise ValueError("Missing required environment variables.")
        
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
                jobs[job_id]["status"] = "PROCESSING_META"
                jobs[job_id]["log"] = "Waiting for Meta to process..."
                
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
            
            # Update draft status
            drafts[container_id]["status"] = "published"
            drafts[container_id]["media_id"] = media_id
            drafts[container_id]["published_at"] = datetime.datetime.utcnow().isoformat()
            
    except Exception as e:
        jobs[job_id]["status"] = "FAILED"
        jobs[job_id]["log"] = f"Error: {str(e)}"
        drafts[container_id]["status"] = "publish_failed"


@app.get("/drafts")
async def list_drafts():
    """List all stored drafts."""
    return {"drafts": drafts}


@app.get("/drafts/{container_id}")
async def get_draft(container_id: str):
    """Get details of a specific draft."""
    if container_id not in drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    return drafts[container_id]


@app.delete("/drafts/{container_id}")
async def delete_draft(container_id: str):
    """Delete a draft."""
    if container_id not in drafts:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    del drafts[container_id]
    return {"message": f"Draft {container_id} deleted successfully"}


# Webhook verification and handling
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "default_verify_token")


@app.get("/webhooks/instagram")
async def verify_webhook(request: Request):
    """Verify webhook subscription from Instagram/Meta."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        return int(challenge) if challenge.isdigit() else challenge
    
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhooks/instagram")
async def handle_webhook(request: Request):
    """Handle incoming webhook events from Instagram/Meta."""
    try:
        # Verify signature if app secret is configured
        app_secret = os.environ.get("IG_APP_SECRET")
        if app_secret:
            x_hub_signature = request.headers.get("X-Hub-Signature-256", "")
            body = await request.body()
            
            if x_hub_signature:
                expected_signature = hmac.new(
                    app_secret.encode(),
                    body,
                    hashlib.sha256
                ).hexdigest()
                
                provided_signature = x_hub_signature.replace("sha256=", "")
                
                if not hmac.compare_digest(expected_signature, provided_signature):
                    raise HTTPException(status_code=403, detail="Invalid signature")
        
        payload = await request.json()
        
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
        
    except Exception as e:
        # Log error but return a generic message to avoid exposing internals
        print(f"Webhook processing error: {str(e)}")
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
        for job_id, job in jobs.items():
            if job.get("container_id") == container_id:
                job["status"] = f"META_{status}"
                job["log"] = f"Meta status updated via webhook: {status}"
                break
        
        # Update draft status if applicable
        if container_id in drafts:
            drafts[container_id]["meta_status"] = status
            drafts[container_id]["status_updated_at"] = datetime.datetime.utcnow().isoformat()


@app.get("/webhooks/config")
async def get_webhook_config():
    """Get current webhook configuration info."""
    return {
        "webhook_url": os.environ.get("WEBHOOK_URL", "Not configured"),
        "verify_token_configured": bool(os.environ.get("WEBHOOK_VERIFY_TOKEN")),
        "app_secret_configured": bool(os.environ.get("IG_APP_SECRET")),
        "setup_instructions": [
            "1. Go to Facebook Developer Dashboard",
            "2. Select your app and navigate to Instagram Graph API",
            "3. Add a webhook subscription for 'instagram' object",
            "4. Set callback URL to: https://your-domain.com/webhooks/instagram",
            "5. Use the WEBHOOK_VERIFY_TOKEN environment variable as verify token",
            "6. Subscribe to fields: media_status, comments, mentions"
        ]
    }

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read())