# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GitHub App installation token minting for CodeMender Agent.

A GitHub App authenticates in two steps:

1. The App signs a short-lived JWT (RS256, at most 10 minutes) with its
   private key. The JWT only grants access to the App's own `/app/...`
   endpoints.
2. The App exchanges the JWT for an installation access token, scoped to one
   installation (an organization or user account) and, here, to the single
   repository being scanned. Installation tokens expire after one hour.

Scans can run for many hours, so a token minted when the container starts
can expire long before the fix branches are pushed. `InstallationTokenProvider`
caches the current token and mints a fresh one once less than
`REFRESH_MARGIN_SECONDS` of its lifetime remains; callers re-read the token
right before each batch of GitHub calls instead of holding on to one string.

Installation tokens are used exactly like a personal access token: as a
`Bearer` token for the REST API and as the `x-access-token` password for git
over HTTPS. Branch pushes, pull requests, comments and commit statuses made
with them are attributed to the App's bot account (`<app-slug>[bot]`). The
author of the fix commits themselves is still the git identity the runners
configure locally.

Minting a token is not a write to the repository, so it also happens in a dry
run (CODEMENDER_DRY_RUN): the clone still needs credentials.

`google-auth` (a dependency of the Google Cloud client libraries) is only
imported when a JWT is actually signed, so deployments that authenticate with
a static token never load it.
"""

import dataclasses
import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from codemender_agent.utils import retry_on_exception

logger = logging.getLogger("codemender-orchestrator")

GITHUB_API_URL = "https://api.github.com"

# GitHub rejects App JWTs whose lifetime exceeds 10 minutes. `iat` is
# backdated to absorb clock drift between this host and GitHub.
JWT_CLOCK_SKEW_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60

# Mint a new installation token once less than this much lifetime remains.
# Installation tokens live for 60 minutes, so a token is reused for roughly
# the first 50 minutes and replaced after that. The margin must comfortably
# exceed the longest run of GitHub calls made with one token read (a push
# followed by pull request creation and a comment).
REFRESH_MARGIN_SECONDS = 10 * 60

# When a refresh fails, a cached token with at least this much lifetime left
# is still returned so that a transient GitHub outage does not fail a scan
# that holds a perfectly usable token.
MIN_USABLE_SECONDS = 60

# Fallback lifetime used only if GitHub omits or garbles `expires_at`. Kept
# well under the documented 60 minutes so the token is refreshed early.
_FALLBACK_TOKEN_LIFETIME_SECONDS = 30 * 60

_REQUEST_TIMEOUT_SECONDS = 30

_PRIVATE_KEY_MARKER = "PRIVATE KEY-----"


class GitHubAppAuthError(RuntimeError):
  """A GitHub App configuration or authentication failure.

  Deliberately not a `requests.RequestException`, so the retry decorator does
  not retry failures that cannot succeed on a second attempt (a malformed key,
  an App that is not installed on the repository, a revoked App, ...).
  """


def normalize_private_key(raw_key: str) -> str:
  """Returns the PEM private key with real newlines.

  Keys pasted into a single-line field often arrive with literal `\\n`
  sequences instead of newlines; those are converted back. Surrounding
  whitespace and quotes are stripped.
  """
  key = (raw_key or "").strip().strip("'\"").strip()
  if "\n" not in key and "\\n" in key:
    key = key.replace("\\n", "\n")
  key = key.replace("\r\n", "\n")
  return key + "\n" if key and not key.endswith("\n") else key


@dataclasses.dataclass(frozen=True)
class GitHubAppCredentials:
  """The static identity of a GitHub App installation.

  Attributes:
    app_id: The App ID (or client ID) shown on the App's settings page. Used
      as the JWT issuer.
    private_key: The App's PEM-encoded RSA private key. Never logged.
    installation_id: The installation to mint tokens for. When None, it is
      looked up from the repository being scanned, which requires the App to
      be installed on that repository.
  """

  app_id: str
  private_key: str = dataclasses.field(repr=False)
  installation_id: Optional[int] = None

  @classmethod
  def from_values(
      cls,
      app_id: Optional[str],
      private_key: Optional[str],
      installation_id: Optional[str] = None,
  ) -> "GitHubAppCredentials":
    """Validates raw configuration values and builds the credentials.

    Raises:
      GitHubAppAuthError: A required value is missing or malformed. The
        message names the environment variable to fix but never echoes the
        private key.
    """
    clean_app_id = (app_id or "").strip()
    if not clean_app_id:
      raise GitHubAppAuthError(
          "GITHUB_APP_ID is required when GitHub App authentication is"
          " configured."
      )
    if any(ch.isspace() for ch in clean_app_id):
      raise GitHubAppAuthError("GITHUB_APP_ID must not contain whitespace.")

    key = normalize_private_key(private_key or "")
    if not key:
      raise GitHubAppAuthError(
          "GITHUB_APP_PRIVATE_KEY is required when GitHub App authentication"
          " is configured."
      )
    if _PRIVATE_KEY_MARKER not in key:
      raise GitHubAppAuthError(
          "GITHUB_APP_PRIVATE_KEY does not look like a PEM private key"
          " (expected a '-----BEGIN RSA PRIVATE KEY-----' block)."
      )

    parsed_installation_id: Optional[int] = None
    raw_installation_id = (installation_id or "").strip()
    if raw_installation_id:
      if not raw_installation_id.isdigit() or int(raw_installation_id) <= 0:
        raise GitHubAppAuthError(
            "GITHUB_APP_INSTALLATION_ID must be a positive integer, got"
            f" '{raw_installation_id}'."
        )
      parsed_installation_id = int(raw_installation_id)

    return cls(
        app_id=clean_app_id,
        private_key=key,
        installation_id=parsed_installation_id,
    )


def build_app_jwt(
    credentials: GitHubAppCredentials, now: Optional[float] = None
) -> str:
  """Signs the short-lived App JWT used to call the `/app/...` endpoints."""
  # pylint: disable=g-import-not-at-top
  from google.auth import crypt as google_crypt
  from google.auth import jwt as google_jwt
  # pylint: enable=g-import-not-at-top

  issued = int(time.time() if now is None else now)
  payload = {
      "iat": issued - JWT_CLOCK_SKEW_SECONDS,
      "exp": issued + JWT_LIFETIME_SECONDS,
      "iss": credentials.app_id,
  }
  try:
    signer = google_crypt.RSASigner.from_string(credentials.private_key)
  except Exception as e:  # pylint: disable=broad-exception-caught
    # The underlying error can quote key material; report only its type.
    raise GitHubAppAuthError(
        "GITHUB_APP_PRIVATE_KEY could not be loaded as an RSA private key"
        f" ({type(e).__name__})."
    ) from None
  encoded = google_jwt.encode(signer, payload, header={"alg": "RS256"})
  return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


def _parse_expires_at(value: Any, now: float) -> float:
  """Converts GitHub's `expires_at` timestamp to epoch seconds."""
  if isinstance(value, str) and value.strip():
    try:
      parsed = datetime.datetime.fromisoformat(
          value.strip().replace("Z", "+00:00")
      )
      if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
      return parsed.timestamp()
    except ValueError:
      pass
  logger.warning(
      "GitHub App token response had no parseable expires_at (%r); assuming"
      " a %d-minute lifetime.",
      value,
      _FALLBACK_TOKEN_LIFETIME_SECONDS // 60,
  )
  return now + _FALLBACK_TOKEN_LIFETIME_SECONDS


def _error_detail(resp: requests.Response) -> str:
  """Extracts GitHub's error message from a response without echoing headers."""
  try:
    body = resp.json()
    if isinstance(body, dict) and body.get("message"):
      return str(body["message"])
  except ValueError:
    pass
  return (resp.text or "").strip()[:200]


@retry_on_exception(max_tries=3, initial_delay=2, backoff_factor=2)
def _app_request(
    session: Any,
    method: str,
    path: str,
    app_jwt: str,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
  """Calls a GitHub App endpoint with the App JWT.

  Rate limiting and server errors raise `requests.HTTPError`, which the
  decorator retries. Any other non-2xx response raises `GitHubAppAuthError`
  immediately.
  """
  resp = session.request(
      method,
      f"{GITHUB_API_URL}{path}",
      headers={
          "Authorization": f"Bearer {app_jwt}",
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "codemender-orchestrator",
      },
      json=json_body,
      timeout=_REQUEST_TIMEOUT_SECONDS,
  )
  if resp.status_code >= 500 or resp.status_code == 429:
    resp.raise_for_status()
  if resp.status_code >= 400:
    raise GitHubAppAuthError(
        f"GitHub App request {method} {path} failed with HTTP"
        f" {resp.status_code}: {_error_detail(resp)}"
    )
  try:
    data = resp.json()
  except ValueError:
    data = None
  if not isinstance(data, dict):
    raise GitHubAppAuthError(
        f"GitHub App request {method} {path} returned an unexpected body."
    )
  return data


class InstallationTokenProvider:
  """Mints, caches and refreshes installation tokens for one repository."""

  def __init__(
      self,
      credentials: GitHubAppCredentials,
      owner: str,
      repo: str,
      *,
      clock: Callable[[], float] = time.time,
      session: Optional[Any] = None,
  ) -> None:
    self._credentials = credentials
    self._owner = owner
    self._repo = repo
    self._clock = clock
    self._session = session if session is not None else requests
    self._installation_id: Optional[int] = credentials.installation_id
    self._token: Optional[str] = None
    self._expires_at: float = 0.0
    self._lock = threading.Lock()

  @property
  def expires_at(self) -> float:
    """Epoch seconds at which the cached token expires (0 when none)."""
    return self._expires_at

  def get_token(self) -> str:
    """Returns a token with at least `REFRESH_MARGIN_SECONDS` of life left.

    Falls back to the cached token when a refresh fails but the cached token
    is still usable for a little while.

    Raises:
      GitHubAppAuthError: No usable token could be obtained.
    """
    with self._lock:
      now = self._clock()
      if self._token and self._expires_at - now > REFRESH_MARGIN_SECONDS:
        return self._token
      try:
        self._token, self._expires_at = self._mint(now)
      except Exception as e:  # pylint: disable=broad-exception-caught
        if self._token and self._expires_at - now > MIN_USABLE_SECONDS:
          logger.warning(
              "Could not refresh the GitHub App installation token for %s/%s"
              " (%s); reusing the cached token, which expires in %d seconds.",
              self._owner,
              self._repo,
              e,
              int(self._expires_at - now),
          )
          return self._token
        if isinstance(e, GitHubAppAuthError):
          raise
        raise GitHubAppAuthError(
            "Could not mint a GitHub App installation token for"
            f" {self._owner}/{self._repo}: {e}"
        ) from e
      return self._token

  def _mint(self, now: float) -> Tuple[str, float]:
    app_jwt = build_app_jwt(self._credentials, now=now)
    if self._installation_id is None:
      self._installation_id = self._lookup_installation_id(app_jwt)
    data = _app_request(
        self._session,
        "POST",
        f"/app/installations/{self._installation_id}/access_tokens",
        app_jwt,
        # Scope the token to the repository being scanned, even when the App
        # is installed on the whole organization.
        json_body={"repositories": [self._repo]},
    )
    token = data.get("token")
    if not token or not isinstance(token, str):
      raise GitHubAppAuthError(
          "GitHub App access token response did not contain a token."
      )
    expires_at = _parse_expires_at(data.get("expires_at"), now)
    logger.info(
        "Minted a GitHub App installation token for %s/%s (installation %s,"
        " valid for %d minutes).",
        self._owner,
        self._repo,
        self._installation_id,
        max(0, int((expires_at - now) // 60)),
    )
    return token, expires_at

  def _lookup_installation_id(self, app_jwt: str) -> int:
    try:
      data = _app_request(
          self._session,
          "GET",
          f"/repos/{self._owner}/{self._repo}/installation",
          app_jwt,
      )
    except GitHubAppAuthError as e:
      raise GitHubAppAuthError(
          f"GitHub App {self._credentials.app_id} does not appear to be"
          f" installed on {self._owner}/{self._repo}. Install the App on the"
          " repository or set GITHUB_APP_INSTALLATION_ID. Details:"
          f" {e}"
      ) from None
    installation_id = data.get("id")
    if not isinstance(installation_id, int) or installation_id <= 0:
      raise GitHubAppAuthError(
          f"GitHub returned no installation ID for {self._owner}/{self._repo}."
      )
    return installation_id


_providers: Dict[
    Tuple[str, Optional[int], str, str], InstallationTokenProvider
] = {}
_providers_lock = threading.Lock()


def get_installation_token(
    credentials: GitHubAppCredentials, owner: str, repo: str
) -> str:
  """Returns a fresh-enough installation token for `owner/repo`.

  Providers are cached per process, so calling this before every batch of
  GitHub operations is cheap: it only contacts GitHub when the cached token
  is close to expiry.
  """
  key = (credentials.app_id, credentials.installation_id, owner, repo)
  with _providers_lock:
    provider = _providers.get(key)
    if provider is None:
      provider = InstallationTokenProvider(credentials, owner, repo)
      _providers[key] = provider
  return provider.get_token()


def reset_token_cache() -> None:
  """Drops every cached provider and token. Intended for tests."""
  with _providers_lock:
    _providers.clear()
