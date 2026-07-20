"""Unit tests for codemender_agent.storage module."""

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.storage import upload_and_sign_report


class TestStorage(unittest.TestCase):

  @patch("codemender_agent.storage.storage.Client")
  def test_upload_and_sign_report(self, mock_client_cls):
    """Test GCS upload and signed URL generation."""
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob
    mock_blob.generate_signed_url.return_value = (
        "https://storage.googleapis.com/signed-url"
    )

    with tempfile.NamedTemporaryFile(suffix=".html") as temp_file:
      url = upload_and_sign_report(
          temp_file.name, "my-bucket", "reports/r.html"
      )
      self.assertEqual(url, "https://storage.googleapis.com/signed-url")
      mock_blob.upload_from_filename.assert_called_once_with(
          temp_file.name, content_type="text/html"
      )


if __name__ == "__main__":
  unittest.main()
