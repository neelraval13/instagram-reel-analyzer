"""Instagram reel URL validation.

We accept the common URL shapes Instagram serves today:

    https://www.instagram.com/reel/<shortcode>/
    https://www.instagram.com/reels/<shortcode>/
    https://www.instagram.com/p/<shortcode>/               (post form)
    https://www.instagram.com/<username>/reel/<shortcode>/

Query strings (?igsh=...) and trailing slashes are tolerated.

Validation exists primarily to defend against SSRF - without it, yt-dlp
would happily try to resolve arbitrary URLs including cloud metadata
endpoints like http://169.254.169.254/.
"""

import logging
import re
from typing import Literal

from app.errors import InvalidReelURLError

logger = logging.getLogger(__name__)

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
                "reel_url_validated",
                extra={"url_form": form, "shortcode": shortcode},
            )
            return url

    logger.warning("reel_url_rejected", extra={"url": url})
    raise InvalidReelURLError(
        f"Not a recognized Instagram reel URL: {url!r}. "
        "Expected instagram.com/reel/<id>, /reels/<id>, /p/<id>, "
        "or /<user>/reel/<id>."
    )
