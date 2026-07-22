"""Send the daily report as a multipart HTML email with inline (CID) charts.

Uses Gmail SMTP (smtp.gmail.com:587, STARTTLS) authenticated with a Google
App Password. A dry-run mode writes the assembled message to a .eml file
locally instead of sending, so the full pipeline can be exercised without
credentials or network.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class InlineImage:
    cid: str          # the token used in the HTML as cid:<cid>
    path: Path


def build_message(*, subject: str, html: str, sender: str, recipient: str,
                  cc: list[str] | None = None,
                  images: list[InlineImage] | None = None) -> EmailMessage:
    """Assemble a multipart/related HTML email with inline images.

    The HTML references images as `cid:<cid>`; we attach each with a matching
    Content-ID so email clients render them inline.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    msg.set_content("This report requires an HTML-capable email client.")

    # Rewrite cid:<TICKER> references to real, unique Content-IDs.
    cid_map: dict[str, str] = {}
    html_out = html
    for img in images or []:
        real_cid = make_msgid(domain="portfolio.monitor")[1:-1]  # strip <>
        cid_map[img.cid] = real_cid
        html_out = html_out.replace(f"cid:{img.cid}", f"cid:{real_cid}")

    msg.add_alternative(html_out, subtype="html")
    html_part = msg.get_payload()[-1]  # the HTML alternative we just added

    for img in images or []:
        data = Path(img.path).read_bytes()
        html_part.add_related(data, maintype="image", subtype="png",
                              cid=f"<{cid_map[img.cid]}>",
                              filename=f"{img.cid}.png")
    return msg


def send_via_gmail(msg: EmailMessage, *, host: str, port: int,
                   user: str, app_password: str,
                   recipients: list[str]) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as server:
        server.starttls(context=context)
        server.login(user, app_password)
        server.send_message(msg, from_addr=user, to_addrs=recipients)
    log.info("Email sent to %s", ", ".join(recipients))


def save_eml(msg: EmailMessage, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(msg))
    return out_path
