import base64
from io import BytesIO

import qrcode


def qr_data_uri(value: str) -> str:
    """Renders `value` as a QR code PNG, base64-encoded as a data: URI.

    Generated server-side (rather than via a client-side JS library) so the
    public projection page -- plain server-rendered HTML/JS with no npm
    toolchain, meant to keep working reliably during a live service -- can
    show a real scannable code without loading any third-party script.
    """
    image = qrcode.make(value)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
