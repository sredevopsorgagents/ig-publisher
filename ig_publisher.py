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


def get_profile_info(ig_user_id: str, access_token: str) -> dict:
    """Retrieves Instagram Business account profile information."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}"
    params = {
        "fields": "id,username,name,biography,followers_count,follows_count,media_count,website,profile_picture_url"
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    print(f"[*] Fetching profile info for user {ig_user_id}...")
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()

    profile_data = response.json()
    print(f"[+] Profile retrieved: @{profile_data.get('username')}")
    return profile_data


def get_content_publishing_limit(ig_user_id: str, access_token: str) -> dict:
    """Checks the content publishing limit status for the Instagram account."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/content_publishing_limit"
    headers = {"Authorization": f"Bearer {access_token}"}

    print(f"[*] Checking content publishing limits for user {ig_user_id}...")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    limit_data = response.json()
    config = limit_data.get("config", {})
    quota_usage = limit_data.get("quota_usage", 0)
    quota_total = config.get("daily_quota", 0)
    quota_remaining = quota_total - quota_usage if quota_total else None

    result = {
        "config": config,
        "quota_usage": quota_usage,
        "quota_total": quota_total,
        "quota_remaining": quota_remaining,
    }

    print(f"[+] Publishing limit: {quota_usage}/{quota_total} used ({quota_remaining} remaining)")
    return result


def create_story_container(
    ig_user_id: str,
    access_token: str,
    media_url: str,
    mime_type: str,
) -> str:
    """Creates a media container for an Instagram Story."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media"

    payload = {}

    if mime_type.startswith("image/"):
        payload["image_url"] = media_url
        payload["media_type"] = "STORY"
    elif mime_type.startswith("video/"):
        payload["video_url"] = media_url
        payload["media_type"] = "STORY_VIDEO"
    else:
        raise ValueError(
            f"Unsupported media type: {mime_type}. Use JPEG/PNG or MP4/MOV."
        )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    print(f"[*] Creating story container...")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    container_id = response.json().get("id")
    print(f"[+] Story container created with ID: {container_id}")
    return container_id


def publish_story(ig_user_id: str, access_token: str, container_id: str) -> str:
    """Publishes the story container to the Instagram account."""
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media_publish"
    payload = {"creation_id": container_id}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    print(f"[*] Publishing story container {container_id}...")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    media_id = response.json().get("id")
    print(f"[+] Successfully published story! Media ID: {media_id}")
    return media_id


def main():
    parser = argparse.ArgumentParser(
        description="Publish content to Instagram via GCS."
    )
    
    # Action mode selection
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--profile", action="store_true", help="Fetch profile information")
    mode_group.add_argument("--check-limits", action="store_true", help="Check content publishing limits")
    mode_group.add_argument("--story", action="store_true", help="Publish as an Instagram Story")
    mode_group.add_argument("file_path", nargs="?", help="Path to the local image or video file (for regular posts)")
    
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

    try:
        # Profile info mode
        if args.profile:
            profile = get_profile_info(ig_user_id, access_token)
            print("\n--- Profile Information ---")
            for key, value in profile.items():
                print(f"{key}: {value}")
            return

        # Content publishing limit check mode
        if args.check_limits:
            limits = get_content_publishing_limit(ig_user_id, access_token)
            print("\n--- Publishing Limits ---")
            print(f"Daily Quota: {limits['quota_total']}")
            print(f"Quota Used: {limits['quota_usage']}")
            print(f"Quota Remaining: {limits['quota_remaining']}")
            return

        # Story publishing mode
        if args.story:
            if not args.file_path:
                sys.exit("Error: --story mode requires a file path.")
            if not os.path.exists(args.file_path):
                sys.exit(f"Error: File not found at {args.file_path}")

            mime_type, _ = mimetypes.guess_type(args.file_path)
            if not mime_type:
                sys.exit("Error: Could not determine the MIME type of the file.")

            # Check limits before publishing story
            print("[*] Checking publishing limits before story upload...")
            limits = get_content_publishing_limit(ig_user_id, access_token)
            if limits['quota_remaining'] is not None and limits['quota_remaining'] <= 0:
                sys.exit("Error: Daily publishing quota exceeded. Cannot publish story.")

            # 1. Upload to GCS and get Signed URL
            public_url = get_signed_url(args.file_path)

            # 2. Create Story Container
            container_id = create_story_container(
                ig_user_id, access_token, public_url, mime_type
            )

            # 3. Wait for Processing
            wait_for_container_readiness(container_id, access_token)

            # 4. Publish Story
            publish_story(ig_user_id, access_token, container_id)
            return

        # Regular post mode (default when file_path is provided)
        if args.file_path:
            if not os.path.exists(args.file_path):
                sys.exit(f"Error: File not found at {args.file_path}")

            mime_type, _ = mimetypes.guess_type(args.file_path)
            if not mime_type:
                sys.exit("Error: Could not determine the MIME type of the file.")

            # Check limits before publishing
            print("[*] Checking publishing limits before upload...")
            limits = get_content_publishing_limit(ig_user_id, access_token)
            if limits['quota_remaining'] is not None and limits['quota_remaining'] <= 0:
                sys.exit("Error: Daily publishing quota exceeded. Cannot publish post.")

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
        else:
            parser.print_help()
            sys.exit(1)

    except requests.exceptions.HTTPError as e:
        print(f"[-] API Error: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"[-] Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
