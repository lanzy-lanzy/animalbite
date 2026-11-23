"""
QR code generation helpers for patient credentials and record verification.

Two payload styles are produced here, both rendered spec-compliant so phone
cameras and jsQR reliably detect them:

* Login-credential QRs (one-time, right after creation/reset while the
  plaintext temp password is still in hand) — plain text with the portal URL,
  username and temp password.
* Verification QRs — host-independent tokens (`ABTC-PATIENT:<number>`,
  `ABTC-PRE-REG:<number>`) resolved against the live database by the in-app
  scanner (`patients:scan_resolve`) on every scan, so a scan always shows
  fresh data and stays valid across localhost, LAN and production. A
  pre-registration token automatically follows conversion to the official
  patient record.

Rendering fixes applied: 4-module quiet zone (the QR spec minimum — a 2-module
border made many scanners fail to detect the code), pure black modules on
white for maximum contrast, and explicit UTF-8 payload bytes.
"""
import io
import base64
import re
import textwrap

# The QR spec requires a 4-module white quiet zone around the symbol; codes
# rendered with a smaller border are frequently rejected by camera scanners.
QUIET_ZONE_MODULES = 4


def build_credentials_payload(login_url, username, password, patient_name=None):
    """Human-readable multi-line text embedded in the QR code."""
    lines = []
    lines.append(f"ABTC Patient Portal — {patient_name}" if patient_name else "ABTC Patient Portal")
    lines.append(f"Login: {login_url}")
    lines.append(f"Username: {username}")
    lines.append(f"Temp Password: {password}")
    lines.append("Change password after first login (Profile > Change Password).")
    return "\n".join(lines)


def build_patient_payload(patient):
    """Portable verification token for a patient's QR (resolved to the live
    record on every scan — never embeds a host or stale data)."""
    return f"ABTC-PATIENT:{patient.patient_number}"


def build_pre_registration_payload(number):
    """Portable verification token for a pre-registration slip QR."""
    return f"ABTC-PRE-REG:{number}"


def qr_png_bytes(text, box_size=8, border=QUIET_ZONE_MODULES, fill_color="#000000",
                 back_color="white", error_correction=None):
    """Render `text` as PNG bytes. Accepts str (encoded as UTF-8) or bytes."""
    import qrcode
    import qrcode.constants

    qr = qrcode.QRCode(
        box_size=box_size,
        border=border,
        error_correction=error_correction or qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(text.encode("utf-8") if isinstance(text, str) else text)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill_color, back_color=back_color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_data_uri(text, **kwargs):
    """Return a PNG QR code for `text` as a base64 data URI (ready for <img src>)."""
    b64 = base64.b64encode(qr_png_bytes(text, **kwargs)).decode("ascii")
    return f"data:image/png;base64,{b64}"


def qr_slip_filename(reference, full_name=""):
    """Build a safe PNG filename containing both reference number and patient name."""
    ref = re.sub(r"[^A-Za-z0-9_-]+", "_", (reference or "record").strip()).strip("_")
    name = re.sub(r"[^A-Za-z0-9]+", "_", (full_name or "").strip()).strip("_")
    if name:
        return f"ABTC_QR_Slip_{ref}_{name}.png"
    return f"ABTC_QR_Slip_{ref}.png"


def _load_font(size, bold=False):
    from PIL import ImageFont

    candidates = (
        ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
        if bold
        else ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def qr_slip_png_bytes(
    payload,
    *,
    title="QR Identification Slip",
    full_name="",
    reference_label="Reference No.",
    reference_value="",
    details=None,
    footer="Present this QR slip at the Animal Bite Treatment Center.",
    error_correction=None,
):
    """Render a printable/downloadable QR slip PNG containing the scannable QR
    code along with the patient's name and key record details."""
    from PIL import Image, ImageDraw

    width = 760
    pad_x = 44
    header_h = 112

    font_eyebrow = _load_font(15, bold=True)
    font_title = _load_font(24, bold=True)
    font_name = _load_font(26, bold=True)
    font_ref = _load_font(17, bold=True)
    font_label = _load_font(15, bold=True)
    font_value = _load_font(16, bold=False)
    font_footer = _load_font(14, bold=False)

    # Render crisp QR with full 4-module quiet zone so every scanner reads it.
    qr_raw = qr_png_bytes(
        payload,
        box_size=9,
        border=QUIET_ZONE_MODULES,
        error_correction=error_correction,
    )
    qr_img = Image.open(io.BytesIO(qr_raw)).convert("RGB")
    qr_target = 300
    qr_img = qr_img.resize((qr_target, qr_target), Image.Resampling.NEAREST)

    # Prepare wrapped detail rows.
    clean_details = []
    for label, value in (details or []):
        val_str = str(value).strip() if value is not None else ""
        if not val_str:
            val_str = "—"
        wrapped = textwrap.wrap(val_str, width=38) or ["—"]
        clean_details.append((str(label).strip(), wrapped))

    row_line_h = 22
    row_pad_y = 12
    details_box_h = 20
    for _, lines in clean_details:
        details_box_h += max(len(lines), 1) * row_line_h + row_pad_y * 2
    if not clean_details:
        details_box_h = 0

    name_block_h = 84
    qr_block_h = qr_target + 36
    footer_h = 64
    total_h = header_h + name_block_h + qr_block_h + details_box_h + footer_h + 36

    canvas = Image.new("RGB", (width, total_h), "#FBF9F4")
    draw = ImageDraw.Draw(canvas)

    # Outer card border
    draw.rectangle([(0, 0), (width - 1, total_h - 1)], outline="#D8D1C2", width=2)

    # Header band (ABTC emerald + gold accent line)
    draw.rectangle([(0, 0), (width, header_h)], fill="#08291F")
    draw.rectangle([(0, header_h - 4), (width, header_h)], fill="#C9A227")
    draw.text(
        (width // 2, 30),
        "MUNICIPAL HEALTH OFFICE  ·  ANIMAL BITE TREATMENT CENTER",
        fill="#D6E6DF",
        font=font_eyebrow,
        anchor="mm",
    )
    draw.text(
        (width // 2, 70),
        title.upper(),
        fill="#FFFFFF",
        font=font_title,
        anchor="mm",
    )

    # Patient Name & Reference Number
    y = header_h + 30
    display_name = (full_name or "Patient").strip()
    draw.text((width // 2, y), display_name, fill="#08291F", font=font_name, anchor="mm")
    y += 34
    if reference_value:
        ref_text = f"{reference_label}: {reference_value}"
        draw.text((width // 2, y), ref_text, fill="#157A61", font=font_ref, anchor="mm")

    # QR Code inside bordered white frame
    qr_frame_pad = 12
    qr_x = (width - qr_target) // 2
    qr_y = header_h + name_block_h + 12
    draw.rounded_rectangle(
        [
            (qr_x - qr_frame_pad, qr_y - qr_frame_pad),
            (qr_x + qr_target + qr_frame_pad, qr_y + qr_target + qr_frame_pad),
        ],
        radius=16,
        fill="#FFFFFF",
        outline="#D8D1C2",
        width=2,
    )
    canvas.paste(qr_img, (qr_x, qr_y))

    # Details panel
    y = qr_y + qr_target + qr_frame_pad + 24
    if clean_details:
        box_left = pad_x
        box_right = width - pad_x
        box_bottom = y + details_box_h - 12
        draw.rounded_rectangle(
            [(box_left, y), (box_right, box_bottom)],
            radius=14,
            fill="#FFFFFF",
            outline="#E3DCCB",
            width=2,
        )
        cur_y = y + 8
        for idx, (label, lines) in enumerate(clean_details):
            if idx > 0:
                draw.line(
                    [(box_left + 18, cur_y), (box_right - 18, cur_y)],
                    fill="#EFE9DC",
                    width=1,
                )
            text_y = cur_y + row_pad_y
            draw.text((box_left + 20, text_y), label, fill="#53645D", font=font_label)
            for line_idx, line in enumerate(lines):
                draw.text(
                    (box_left + 245, text_y + line_idx * row_line_h),
                    line,
                    fill="#0F1F1A",
                    font=font_value,
                )
            cur_y += max(len(lines), 1) * row_line_h + row_pad_y * 2
        y = box_bottom + 20

    # Footer reminder
    draw.text(
        (width // 2, total_h - 30),
        footer,
        fill="#53645D",
        font=font_footer,
        anchor="mm",
    )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
