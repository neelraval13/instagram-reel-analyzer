"""Instagram reel URL validation and prompt validation.

We accept the common Instagram URL shapes Instagram serves today:

    https://www.instagram.com/reel/<shortcode>/
    https://www.instagram.com/reels/<shortcode>/
    https://www.instagram.com/p/<shortcode>/               (post form)
    https://www.instagram.com/<username>/reel/<shortcode>/

Query strings (?igsh=...) and trailing slashes are tolerated.

URL validation exists primarily to defend against SSRF - without it,
yt-dlp would happily try to resolve arbitrary URLs including cloud
metadata endpoints like http://169.254.169.254/.

Prompt validation enforces a sensible character cap and rejects empty
or whitespace-only input. Default prompts are a client concern; the
server stays strict so client bugs surface loudly instead of silently.
"""

import logging
import re
from typing import Literal

from app.errors import InvalidPromptError, InvalidReelURLError

logger = logging.getLogger(__name__)

# Limits. These match the values documented in the API; clients should
# self-restrict ahead of these to keep error responses rare.
PROMPT_MIN_LENGTH = 1
PROMPT_MAX_LENGTH = 2000

UrlForm = Literal["reel", "reels", "post", "user_reel"]

# Four recognized shapes. Each pattern captures the shortcode for logging.
_PATTERNS: dict[UrlForm, re.Pattern[str]] = {
    "reel": re.compile(
        r"^https?://(?:www\.)?instagram\.com/reel/([A-Za-z0-9_-]+)/?",
        re.IGNORECASE,
    ),
    "reels": re.compile(
        r"^https?://(?:www\.)?instagram\.com/reels/([A-Za-z0-9_-]+)/?",
        re.IGNORECASE,
    ),
    "post": re.compile(
        r"^https?://(?:www\.)?instagram\.com/p/([A-Za-z0-9_-]+)/?",
        re.IGNORECASE,
    ),
    "user_reel": re.compile(
        r"^https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+/reel/([A-Za-z0-9_-]+)/?",
        re.IGNORECASE,
    ),
}


def validate_reel_url(url: str) -> str:
    """Validate a reel URL and return it unchanged if valid.

    Logs which URL form was matched, so we can see in aggregate what
    shapes real users send from different share sheets.

    Raises:
        InvalidReelURLError: if the URL doesn't match any known shape.
    """
    for form, pattern in _PATTERNS.items():
        match = pattern.match(url)
        if match:
            shortcode = match.group(1)
            logger.info(
                "url_validated",
                extra={"url_form": form, "shortcode": shortcode},
            )
            return url

    logger.warning("url_rejected", extra={"url": url})
    raise InvalidReelURLError(
        f"Not a recognized Instagram reel URL: {url!r}. "
        "Expected instagram.com/reel/<id>, /reels/<id>, /p/<id>, "
        "or /<user>/reel/<id>."
    )


def validate_prompt(prompt: str) -> str:
    """Validate a prompt string and return it stripped of leading/trailing
    whitespace if valid.

    Empty or whitespace-only prompts are rejected - clients are expected
    to send a meaningful default (e.g. "Summarize this reel") rather
    than relying on the server to invent one.

    Raises:
        InvalidPromptError: if empty, whitespace-only, or over the cap.
    """
    stripped = prompt.strip()

    if not stripped:
        logger.warning("prompt_rejected", extra={"reason": "empty"})
        raise InvalidPromptError(
            "Prompt cannot be empty. Provide a non-blank instruction "
            "(e.g. 'Summarize this reel')."
        )

    if len(prompt) > PROMPT_MAX_LENGTH:
        logger.warning(
            "prompt_rejected",
            extra={"reason": "too_long", "length": len(prompt)},
        )
        raise InvalidPromptError(
            f"Prompt is {len(prompt)} characters; maximum is {PROMPT_MAX_LENGTH}."
        )

    return stripped
