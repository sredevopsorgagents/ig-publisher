#!/usr/bin/env python3
"""
Instagram API Publisher (GCP Edition)
Uploads media to GCS, generates a Signed URL, and publishes to Instagram.
"""

import os
import sys
import time
import argparse
import requests
import mimetypes
import uuid
import datetime
from google.cloud import storage
from google.oauth2 import service_account

GRAPH_API_VERSION = "v24.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"


def get_gcs_client() -> storage.Client:
    """
    Initializes the GCS client using a static JSON Service Account key.
    """
    key_path = os.environ.get("GCP_SA_KEY_PATH")
    if not key_path or not os.path.exists(key_path):
        raise ValueError(f"GCP Service Account key not found at: {key_path}")

    # Explicitly load credentials from the JSON file
    credentials = service_account.Credentials.from_service_account_file(
        key_path, scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
    )

    return storage.Client(credentials=credentials, project=credentials.project_id)


def get_signed_url(file_path: str) -> str:
    """
    Uploads a local file to GCS and generates a V4 Signed URL using the JSON key.
    """
    bucket_name = os.environ.get("GCS_BUCKET_NAME")
    if not bucket_name:
        raise ValueError("GCS_BUCKET_NAME environment variable is required.")

    # Use the explicit JSON key client
    storage_client = get_gcs_client()
    bucket = storage_client.bucket(bucket_name)

    blob_name = f"ig-uploads/{uuid.uuid4()}-{os.path.basename(file_path)}"
    blob = bucket.blob(blob_name)

    print(f"[*] Uploading {file_path} to gs://{bucket_name}/{blob_name}...")
    blob.upload_from_filename(file_path)

    print("[*] Generating V4 Signed URL locally using JSON private key...")
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=60),
        method="GET",
    )

    return url


def create_media_container(
    ig_user_id: str,
    access_token: str,
    media_url: str,
    mime_type: str,
    caption: str = "",
) -> str:
    """Creates a media container for the image or video."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media"

    payload = {"caption": caption}

    if mime_type.startswith("image/"):
        payload["image_url"] = media_url
    elif mime_type.startswith("video/"):
        payload["media_type"] = "REELS"
        payload["video_url"] = media_url
    else:
        raise ValueError(
            f"Unsupported media type: {mime_type}. Use JPEG/PNG or MP4/MOV."
        )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    print(f"[*] Creating media container...")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    container_id = response.json().get("id")
    print(f"[+] Container created with ID: {container_id}")
    return container_id


def wait_for_container_readiness(
    container_id: str, access_token: str, timeout: int = 300
):
    """Polls the container status until it's FINISHED or fails."""
    url = f"{GRAPH_API_BASE}/{container_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"fields": "status_code"}

    print("[*] Waiting for Meta to process the media...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        status = response.json().get("status_code")

        if status == "FINISHED":
            print("[+] Media processing finished successfully.")
            return True
        elif status in ["ERROR", "EXPIRED"]:
            raise RuntimeError(f"Media processing failed with status: {status}")

        print(f"    Status: {status}. Retrying in 60 seconds...")
        time.sleep(60)

    raise TimeoutError("Media processing timed out.")


def publish_media(ig_user_id: str, access_token: str, container_id: str) -> str:
    """Publishes the media container to the Instagram account."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media_publish"
    payload = {"creation_id": container_id}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    print(f"[*] Publishing container {container_id}...")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    media_id = response.json().get("id")
    print(f"[+] Successfully published! Media ID: {media_id}")
    return media_id


def main():
    parser = argparse.ArgumentParser(
        description="Publish content to Instagram via GCS."
    )
    parser.add_argument("file_path", help="Path to the local image or video file.")
    parser.add_argument(
        "--caption", default="", help="Optional text caption for the post."
    )

    args = parser.parse_args()

    ig_user_id = os.environ.get("IG_USER_ID")
    access_token = os.environ.get("IG_ACCESS_TOKEN")

    if not ig_user_id or not access_token:
        sys.exit(
            "Error: Please set IG_USER_ID and IG_ACCESS_TOKEN environment variables."
        )

    if not os.path.exists(args.file_path):
        sys.exit(f"Error: File not found at {args.file_path}")

    mime_type, _ = mimetypes.guess_type(args.file_path)
    if not mime_type:
        sys.exit("Error: Could not determine the MIME type of the file.")

    try:
        # 1. Upload to GCS and get Signed URL
        public_url = get_signed_url(args.file_path)

        # 2. Create Container
        container_id = create_media_container(
            ig_user_id, access_token, public_url, mime_type, args.caption
        )

        # 3. Wait for Processing
        wait_for_container_readiness(container_id, access_token)

        # 4. Publish
        publish_media(ig_user_id, access_token, container_id)

    except requests.exceptions.HTTPError as e:
        print(f"[-] API Error: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"[-] Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
