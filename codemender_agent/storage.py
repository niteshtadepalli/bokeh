"""Cloud Storage operations and report upload utilities for CodeMender Agent."""

import datetime
import logging
import os
from typing import Optional

# pylint: disable=unused-argument
class DummyStorage:
  """Dummy fallback for local developer/unit testing environments."""

  class Blob:

    def upload_from_filename(self, *args, **kwargs):
      pass

    def generate_signed_url(self, *args, **kwargs):
      return ""

  class Bucket:

    def blob(self, *args, **kwargs):
      return DummyStorage.Blob()

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


def upload_and_sign_report(
    local_file_path: str, bucket_name: str, dest_blob_name: str
) -> Optional[str]:
  """Uploads a local HTML report to GCS and returns a temporary Signed URL."""
  if not os.path.exists(local_file_path):
    logger.error("Local report file not found at: %s", local_file_path)
    return None
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

    signing_kwargs = {
        "version": "v4",
        "expiration": datetime.timedelta(days=3),  # 3 days
        "method": "GET",
    }

    # Wrap in Impersonated Credentials for token-only environments (e.g. Cloud Run)
    if hasattr(client, "_credentials"):
      try:
        from google.auth import credentials as auth_credentials
        is_signing = isinstance(client._credentials, auth_credentials.Signing)
      except Exception:  # pylint: disable=broad-exception-caught
        is_signing = False

      if not is_signing:
        sa_email = getattr(client._credentials, "service_account_email", None)

        # Fallback metadata check for sa_email if credentials.service_account_email was unset/default
        if not sa_email or sa_email == "default":
          try:
            import requests
            resp = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
                timeout=2,
            )
            if resp.status_code == 200:
              sa_email = resp.text.strip()
              logger.info(
                  "Automatically fetched service account email from Metadata"
                  " Server: %s",
                  sa_email,
              )
          except Exception:  # pylint: disable=broad-exception-caught
            pass

        if sa_email:
          try:
            from google.auth import impersonated_credentials
            logger.info("Using Impersonated Credentials signer for: %s", sa_email)
            signing_creds = impersonated_credentials.Credentials(
                source_credentials=client._credentials,
                target_principal=sa_email,
                target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
            )
            signing_kwargs["credentials"] = signing_creds
          except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to create impersonated credentials: %s", e)

    url = blob.generate_signed_url(**signing_kwargs)
    return url
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error("Failed to upload or generate signed URL for report: %s", e)
  return None
