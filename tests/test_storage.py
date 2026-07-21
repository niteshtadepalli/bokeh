"""Unit tests for codemender_agent.storage module."""

import datetime
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codemender_agent.storage import upload_and_sign_report


class TestStorage(unittest.TestCase):

  @patch("codemender_agent.storage.storage.Client")
  def test_upload_and_sign_report_with_signing_credentials(
      self, mock_client_cls
  ):
    """Test GCS URL signing when credentials already support signing (e.g. JSON key)."""
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    # Setup mocked google.auth namespaces
    mock_google = MagicMock()
    mock_auth = MagicMock()
    mock_credentials_module = MagicMock()

    class FakeSigning:
      pass

    mock_credentials_module.Signing = FakeSigning
    mock_auth.credentials = mock_credentials_module
    mock_google.auth = mock_auth

    # Create mock credentials that inherit from FakeSigning
    class SigningCredentials(FakeSigning):
      pass

    mock_credentials = SigningCredentials()
    mock_client._credentials = mock_credentials

    mock_blob.generate_signed_url.return_value = (
        "https://storage.googleapis.com/signed-url"
    )

    with patch.dict(
        sys.modules,
        {
            "google": mock_google,
            "google.auth": mock_auth,
            "google.auth.credentials": mock_credentials_module,
        },
    ):
      with tempfile.NamedTemporaryFile(suffix=".html") as temp_file:
        url = upload_and_sign_report(
            temp_file.name, "my-bucket", "reports/r.html"
        )
        self.assertEqual(url, "https://storage.googleapis.com/signed-url")

        # Verify generate_signed_url was called with default kwargs (no wrapped credentials)
        mock_blob.generate_signed_url.assert_called_once_with(
            version="v4",
            expiration=datetime.timedelta(days=3),
            method="GET",
        )

  @patch("codemender_agent.storage.storage.Client")
  def test_upload_and_sign_report_with_impersonated_credentials(
      self, mock_client_cls
  ):
    """Test GCS URL signing using Impersonated Credentials (e.g. Cloud Run)."""
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    # Configure credentials that do NOT inherit from Signing
    mock_credentials = MagicMock()
    mock_credentials.service_account_email = (
        "test-sa@project.iam.gserviceaccount.com"
    )
    mock_client._credentials = mock_credentials

    # Setup mocked google.auth namespaces
    mock_google = MagicMock()
    mock_auth = MagicMock()
    mock_credentials_module = MagicMock()

    class FakeSigning:
      pass

    mock_credentials_module.Signing = FakeSigning
    mock_auth.credentials = mock_credentials_module
    mock_google.auth = mock_auth

    mock_impersonated_module = MagicMock()
    mock_signing_creds = MagicMock()
    mock_impersonated_module.Credentials.return_value = mock_signing_creds
    mock_auth.impersonated_credentials = mock_impersonated_module

    mock_blob.generate_signed_url.return_value = (
        "https://storage.googleapis.com/signed-url"
    )

    with patch.dict(
        sys.modules,
        {
            "google": mock_google,
            "google.auth": mock_auth,
            "google.auth.credentials": mock_credentials_module,
            "google.auth.impersonated_credentials": mock_impersonated_module,
        },
    ):
      with tempfile.NamedTemporaryFile(suffix=".html") as temp_file:
        url = upload_and_sign_report(
            temp_file.name, "my-bucket", "reports/r.html"
        )
        self.assertEqual(url, "https://storage.googleapis.com/signed-url")

        # Verify impersonated credentials wrapper was constructed
        mock_impersonated_module.Credentials.assert_called_once_with(
            source_credentials=mock_credentials,
            target_principal="test-sa@project.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        )

        # Verify generate_signed_url was called with the impersonated signing credentials
        mock_blob.generate_signed_url.assert_called_once_with(
            version="v4",
            expiration=datetime.timedelta(days=3),
            method="GET",
            credentials=mock_signing_creds,
        )


if __name__ == "__main__":
  unittest.main()
