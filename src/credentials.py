"""
Credentials for the premium reader.

Two routes are supported:

    OPENAI    straight to api.openai.com with an OpenAI key
    PORTKEY   through a Portkey gateway, which needs the gateway key plus one
              of a provider, a virtual key, or a saved config id

Precedence is deliberate:

    credentials set at runtime  >  environment variables  >  nothing

The UI collects a key from whoever is using the app and sets it at runtime;
scripts read one from the environment. Rather than thread a credentials object
through pipeline -> extract -> extract_vision, this module holds it and the
vision extractor asks for it.

NOTHING HERE IS EVER WRITTEN TO DISK. A key typed into the UI lives for the
session and then goes away, which is the right default for someone else's
secret. It is also never put in a trace, a log line, or an error message --
see redacted().
"""

import os
from dataclasses import dataclass, replace

from . import config

OPENAI = "openai"
PORTKEY = "portkey"


@dataclass(frozen=True)
class Credentials:
    """How to reach a vision model. Immutable so it cannot drift mid-run."""

    route: str = OPENAI
    api_key: str | None = None
    base_url: str | None = None
    virtual_key: str | None = None
    provider: str | None = None
    config_id: str | None = None
    model: str | None = None

    # -- validation --------------------------------------------------------

    def missing(self) -> str | None:
        """Return a human-readable problem, or None if usable."""
        if self.route == OPENAI:
            if not self.api_key:
                return ("No OpenAI API key. Add one in the sidebar, or set "
                        f"{config.OPENAI_API_KEY_ENV} in .env.")
            return None

        if self.route == PORTKEY:
            if not self.api_key:
                return ("No Portkey API key. Add one in the sidebar, or set "
                        f"{config.PORTKEY_API_KEY_ENV} in .env.")
            if not (self.virtual_key or self.provider or self.config_id):
                return ("Portkey needs to know which provider to route to. "
                        "Give it a provider, a virtual key, or a config id.")
            return None

        return f"Unknown route: {self.route!r}"

    @property
    def is_usable(self) -> bool:
        return self.missing() is None

    def with_model(self, model: str | None) -> "Credentials":
        return replace(self, model=model) if model else self

    def effective_model(self) -> str:
        return self.model or config.OPENAI_VISION_MODEL

    def redacted(self) -> dict:
        """Safe to print, log, or drop into a trace. Never the key itself."""
        def tail(value: str | None) -> str | None:
            if not value:
                return None
            return f"...{value[-4:]}" if len(value) > 4 else "set"

        return {
            "route": self.route,
            "api_key": tail(self.api_key),
            "base_url": self.base_url,
            "virtual_key": tail(self.virtual_key),
            "provider": self.provider,
            "config_id": self.config_id,
            "model": self.effective_model(),
        }

    # -- client ------------------------------------------------------------

    def build_client(self):
        """
        An OpenAI SDK client pointed at the right place.

        Portkey speaks the OpenAI API, so the only differences are the base URL
        and a few x-portkey-* headers. Everything downstream is identical,
        which is why there is no second code path for calling the model.
        """
        problem = self.missing()
        if problem:
            raise RuntimeError(problem)

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The openai package is not installed. "
                "Run: python -m pip install openai"
            ) from exc

        if self.route == OPENAI:
            return OpenAI(api_key=self.api_key,
                          base_url=self.base_url or None)

        headers = {"x-portkey-api-key": self.api_key}
        if self.provider:
            headers["x-portkey-provider"] = self.provider
        if self.virtual_key:
            headers["x-portkey-virtual-key"] = self.virtual_key
        if self.config_id:
            headers["x-portkey-config"] = self.config_id

        # The gateway holds the provider credential, so the SDK's own api_key
        # is unused. It still has to be non-empty or the client refuses to
        # construct.
        return OpenAI(
            api_key="portkey",
            base_url=self.base_url or config.PORTKEY_BASE_URL,
            default_headers=headers,
        )


# ---------------------------------------------------------------------------
# SOURCES
# ---------------------------------------------------------------------------

def from_env() -> Credentials:
    """
    Read credentials from the environment, loading .env if python-dotenv is
    available. A Portkey key present anywhere wins, since nobody sets one by
    accident.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(config.ROOT_DIR / ".env")
    except ImportError:
        pass

    portkey_key = os.environ.get(config.PORTKEY_API_KEY_ENV)
    if portkey_key:
        return Credentials(
            route=PORTKEY,
            api_key=portkey_key,
            base_url=os.environ.get("PORTKEY_BASE_URL") or config.PORTKEY_BASE_URL,
            virtual_key=os.environ.get("PORTKEY_VIRTUAL_KEY") or None,
            provider=os.environ.get("PORTKEY_PROVIDER") or None,
            config_id=os.environ.get("PORTKEY_CONFIG") or None,
            model=os.environ.get("PORTKEY_MODEL") or None,
        )

    return Credentials(
        route=OPENAI,
        api_key=os.environ.get(config.OPENAI_API_KEY_ENV) or None,
        model=os.environ.get("OPENAI_MODEL") or None,
    )


_runtime: Credentials | None = None


def set_credentials(creds: Credentials | None) -> None:
    """Set credentials for this process. The UI calls this; scripts do not."""
    global _runtime
    _runtime = creds


def clear_credentials() -> None:
    set_credentials(None)


def current() -> Credentials:
    """Runtime credentials if any were set, otherwise the environment."""
    return _runtime if _runtime is not None else from_env()


def probe(creds: Credentials | None = None) -> tuple[bool, str]:
    """
    Try the smallest possible real call, so a key can be checked without
    spending a page of image tokens on an invoice.

    Returns (ok, message). Never raises -- a failed probe is information, not
    an error, and the message is what the person needs to fix it.
    """
    creds = creds or current()
    problem = creds.missing()
    if problem:
        return False, problem

    try:
        client = creds.build_client()
        client.chat.completions.create(
            model=creds.effective_model(),
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return True, (f"Connected via {creds.route} to "
                      f"{creds.effective_model()}.")
    except Exception as exc:
        detail = str(exc)
        # Keep it short; SDK errors carry whole request dumps.
        if len(detail) > 300:
            detail = detail[:300] + "..."
        return False, f"{type(exc).__name__}: {detail}"
