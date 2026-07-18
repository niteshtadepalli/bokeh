"""Cloud Storage operations and report upload utilities for CodeMender Agent."""

import datetime
import logging
import os
from typing import Optional

try:
  from google.cloud import storage
except ImportError:
  # Dummy fallback for local developer/unit testing environments
  # pylint: disable=unused-argument
  class DummyStorage:

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

    # Generate signed URL valid for 3 days
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(days=3),  # 3 days
        method="GET",
    )
    return url
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error("Failed to upload or generate signed URL for report: %s", e)
  return None
