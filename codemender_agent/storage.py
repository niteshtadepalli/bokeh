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

    # Retrieve service account email for token-only environments (e.g. Cloud Run)
    if hasattr(client, "_credentials"):
      try:
        import requests
      except ImportError:
        requests = None

      sa_email = None
      if (
          hasattr(client._credentials, "service_account_email")
          and client._credentials.service_account_email
      ):
        sa_email = client._credentials.service_account_email
      elif requests:
        try:
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
        signing_kwargs["service_account_email"] = sa_email

    url = blob.generate_signed_url(**signing_kwargs)
    return url
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error("Failed to upload or generate signed URL for report: %s", e)
  return None
