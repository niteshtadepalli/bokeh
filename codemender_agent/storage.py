"""Cloud Storage operations and report upload utilities for CodeMender Agent."""

import datetime
import logging
import os
import shutil
from typing import Optional
import requests


# pylint: disable=unused-argument
class DummyStorage:
  """Dummy fallback for local developer/unit testing environments."""

  class Blob:

    def upload_from_filename(self, *args, **kwargs):
      pass

    def download_to_filename(self, *args, **kwargs):
      pass

    def generate_signed_url(self, *args, **kwargs):
      return ""

  class Bucket:

    def blob(self, *args, **kwargs):
      return DummyStorage.Blob()

    def list_blobs(self, *args, **kwargs):
      return []

  class Client:

    def __init__(self, *args, **kwargs):
      pass

    def bucket(self, *args, **kwargs):
      return DummyStorage.Bucket()


# pylint: enable=unused-argument

try:
  from google.cloud import storage
except ImportError:
  storage = DummyStorage

logger = logging.getLogger("codemender-orchestrator")


def _get_local_storage_path(bucket_name: str, blob_name: str) -> str:
  storage_dir = os.environ.get(
      "CODEMENDER_LOCAL_STORAGE_DIR", "/tmp/codemender_local_storage"
  )
  return os.path.join(storage_dir, bucket_name, blob_name)


def generate_signed_url(
    bucket_name: str,
    blob_name: str,
    method: str = "GET",
    expiration_days: int = 3,
    content_type: Optional[str] = None,
) -> Optional[str]:
  """Generates a temporary Signed URL for a GCS blob (supports GET/PUT)."""
  if os.environ.get("CODEMENDER_STORAGE_MODE") == "local":
    local_path = _get_local_storage_path(bucket_name, blob_name)
    return f"file://{local_path}"

  try:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    signing_kwargs = {
        "version": "v4",
        "expiration": datetime.timedelta(days=expiration_days),
        "method": method,
    }
    if content_type:
      signing_kwargs["content_type"] = content_type

    # Wrap in Impersonated Credentials for token-only environments
    # (e.g. Cloud Run)
    # pylint: disable=protected-access
    if hasattr(client, "_credentials"):
      try:
        from google.auth import credentials as auth_credentials

        is_signing = isinstance(client._credentials, auth_credentials.Signing)
      except Exception:  # pylint: disable=broad-exception-caught
        is_signing = False

      if not is_signing:
        sa_email = getattr(client._credentials, "service_account_email", None)

        # Fallback metadata check for sa_email if credentials was default
        if not sa_email or sa_email == "default":
          try:
            resp = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/"
                "instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
                timeout=2,
            )
            if resp.status_code == 200:
              sa_email = resp.text.strip()
              logger.info(
                  "Automatically fetched service account email: %s",
                  sa_email,
              )
          except Exception:  # pylint: disable=broad-exception-caught
            pass

        if sa_email:
          try:
            # pylint: disable=import-outside-toplevel
            from google.auth import impersonated_credentials

            logger.info(
                "Using Impersonated Credentials signer for: %s", sa_email
            )
            signing_creds = impersonated_credentials.Credentials(
                source_credentials=client._credentials,
                target_principal=sa_email,
                target_scopes=[
                    "https://www.googleapis.com/auth/devstorage.read_write"
                ],
            )
            signing_kwargs["credentials"] = signing_creds
          except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to create impersonated credentials: %s", e)
    # pylint: enable=protected-access

    url = blob.generate_signed_url(**signing_kwargs)
    return url
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error(
        "Failed to generate signed URL for gs://%s/%s: %s",
        bucket_name,
        blob_name,
        e,
    )
  return None


def upload_and_sign_report(
    local_file_path: str, bucket_name: str, dest_blob_name: str
) -> Optional[str]:
  """Uploads a local HTML report to GCS and returns a temporary Signed URL."""
  if not os.path.exists(local_file_path):
    logger.error("Local report file not found at: %s", local_file_path)
    return None
  if os.environ.get("CODEMENDER_STORAGE_MODE") == "local":
    dest_path = _get_local_storage_path(bucket_name, dest_blob_name)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.copy(local_file_path, dest_path)
    return generate_signed_url(bucket_name, dest_blob_name)
  try:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(dest_blob_name)

    logger.info(
        "Uploading report %s to gs://%s/%s...",
        local_file_path,
        bucket_name,
        dest_blob_name,
    )
    blob.upload_from_filename(local_file_path, content_type="text/html")

    return generate_signed_url(
        bucket_name,
        dest_blob_name,
        method="GET",
        expiration_days=3,
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error("Failed to upload report: %s", e)
  return None


def upload_file_to_gcs(
    local_path: str, bucket_name: str, dest_blob_name: str
) -> bool:
  """Uploads a local file to GCS using standard credentials."""
  if not os.path.exists(local_path):
    logger.error("Local file not found for upload: %s", local_path)
    return False
  if os.environ.get("CODEMENDER_STORAGE_MODE") == "local":
    dest_path = _get_local_storage_path(bucket_name, dest_blob_name)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.copy(local_path, dest_path)
    return True
  try:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(dest_blob_name)
    logger.info(
        "Uploading %s to gs://%s/%s...", local_path, bucket_name, dest_blob_name
    )
    blob.upload_from_filename(local_path)
    return True
  except Exception as e:
    logger.error("Failed to upload %s to GCS: %s", local_path, e)
    return False


def download_file_from_gcs(
    dest_local_path: str, bucket_name: str, src_blob_name: str
) -> bool:
  """Downloads a file from GCS to a local path using standard credentials."""
  if os.environ.get("CODEMENDER_STORAGE_MODE") == "local":
    src_path = _get_local_storage_path(bucket_name, src_blob_name)
    if not os.path.exists(src_path):
      return False
    os.makedirs(os.path.dirname(dest_local_path), exist_ok=True)
    shutil.copy(src_path, dest_local_path)
    return True
  try:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(src_blob_name)
    logger.info(
        "Downloading gs://%s/%s to %s...",
        bucket_name,
        src_blob_name,
        dest_local_path,
    )
    os.makedirs(os.path.dirname(dest_local_path), exist_ok=True)
    blob.download_to_filename(dest_local_path)
    return True
  except Exception as e:
    logger.error(
        "Failed to download gs://%s/%s from GCS: %s",
        bucket_name,
        src_blob_name,
        e,
    )
    return False


def list_gcs_blobs(bucket_name: str, prefix: str) -> list[str]:
  """Lists blobs in a GCS bucket with a given prefix."""
  if os.environ.get("CODEMENDER_STORAGE_MODE") == "local":
    storage_dir = os.environ.get(
        "CODEMENDER_LOCAL_STORAGE_DIR", "/tmp/codemender_local_storage"
    )
    bucket_dir = os.path.join(storage_dir, bucket_name)
    prefix_dir = os.path.join(bucket_dir, prefix)
    if not os.path.exists(prefix_dir):
      return []
    blobs = []
    for root, _, files in os.walk(prefix_dir):
      for file in files:
        full_path = os.path.join(root, file)
        rel_path = os.path.relpath(full_path, bucket_dir)
        blobs.append(rel_path)
    return blobs
  try:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=prefix)
    return [blob.name for blob in blobs]
  except Exception as e:
    logger.error(
        "Failed to list GCS blobs in bucket %s with prefix %s: %s",
        bucket_name,
        prefix,
        e,
    )
    return []


def download_from_url(url: str, dest_path: str) -> bool:
  """Downloads a file from a given URL (e.g., Signed URL) to a local path."""
  if url.startswith("file://"):
    try:
      src_path = url[7:]
      os.makedirs(os.path.dirname(dest_path), exist_ok=True)
      shutil.copy(src_path, dest_path)
      return True
    except Exception as e:
      logger.error(
          "Failed to copy local file from %s to %s: %s", url, dest_path, e
      )
      return False
  try:
    logger.info("Downloading from URL to %s...", dest_path)
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
      for chunk in response.iter_content(chunk_size=8192):
        f.write(chunk)
    return True
  except Exception as e:
    logger.error("Failed to download from URL %s: %s", url, e)
    return False


def upload_to_url(local_path: str, url: str) -> bool:
  """Uploads a local file to a given URL (e.g., Signed PUT URL)."""
  if not os.path.exists(local_path):
    logger.error("Local file not found for upload: %s", local_path)
    return False
  if url.startswith("file://"):
    try:
      dest_path = url[7:]
      os.makedirs(os.path.dirname(dest_path), exist_ok=True)
      shutil.copy(local_path, dest_path)
      return True
    except Exception as e:
      logger.error("Failed to copy local file to %s: %s", url, e)
      return False
  try:
    logger.info("Uploading %s to URL...", local_path)
    with open(local_path, "rb") as f:
      response = requests.put(
          url,
          data=f,
          headers={"Content-Type": "application/octet-stream"},
          timeout=60,
      )
    response.raise_for_status()
    return True
  except Exception as e:
    logger.error("Failed to upload %s to URL: %s", local_path, e)
    return False
