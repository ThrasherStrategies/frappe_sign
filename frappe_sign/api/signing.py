# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import base64
import json
from io import BytesIO

import frappe
from frappe.utils import now_datetime
from PIL import Image, ImageDraw, ImageFont

from frappe_sign.api.request import create_file_hash
from frappe_sign.utils.audit import append_event, sha256_bytes
from frappe_sign.utils.certificates import generate_audit_certificate
from frappe_sign.utils.files import attach_private_file, get_file_bytes
from frappe_sign.utils.notifications import (
    CLOSED_REQUEST_STATUSES,
    send_completion_notification,
    send_progress_notification,
)
from frappe_sign.utils.pdf_stamping import stamp_pdf_fields
from frappe_sign.utils.signatures import attach_signature_png
from frappe_sign.utils.tokens import hash_signing_token
from frappe_sign.utils.pdf_merge import append_pdf_bytes
from frappe_sign.utils.pdf_digital_signing import digitally_sign_pdf
from frappe_sign.utils.certificate_records import create_audit_certificate_record, create_final_verification_package_record


OPEN_SIGNER_STATUSES = ("Pending", "Sent", "Viewed")
CLOSED_SIGNER_STATUSES = ("Signed", "Declined", "Skipped")


def _get_signer_from_token(token):
    if not token:
        frappe.throw("Missing signing token.")

    token_hash = hash_signing_token(token)

    signer = frappe.db.get_value(
        "Frappe Sign Signer",
        {
            "access_token_hash": token_hash,
            "status": ["in", OPEN_SIGNER_STATUSES],
        },
        [
            "name",
            "parent",
            "signer",
            "email",
            "full_name",
            "status",
            "signing_order",
        ],
        as_dict=True,
    )

    if not signer:
        frappe.throw("Invalid or expired signing token.")

    request = frappe.get_doc("Frappe Sign Request", signer.parent)

    if request.status in CLOSED_REQUEST_STATUSES:
        frappe.throw(f"This signing request is {request.status}.")

    return signer, request


def _get_signer_row(request, signer_row_name):
    for row in request.signers:
        if row.name == signer_row_name:
            return row

    return None


def _get_profile_for_signer(signer):
    if not signer.signer:
        frappe.throw("This signer is not linked to a Frappe Sign Profile.")

    profile = frappe.get_doc("Frappe Sign Profile", signer.signer)

    if not profile.active:
        frappe.throw("Your Frappe Sign Profile is not active.")

    return profile


def get_settings():
    return frappe.get_single("Frappe Sign Settings")


def is_consent_required():
    settings = get_settings()
    return bool(settings.require_signature_consent)


def ensure_signer_can_act(request, signer):
    if request.signing_mode != "Sequential":
        return

    signer_order = signer.signing_order or 1
    current_order = request.current_signing_order or 1

    if signer_order > current_order:
        frappe.throw("This signing request is waiting for an earlier signer.")

    if signer_order < current_order and signer.status != "Signed":
        frappe.throw("This signing request is no longer available for this signer.")


@frappe.whitelist(allow_guest=True)
def get_signing_context(token):
    signer, request = _get_signer_from_token(token)

    ensure_signer_can_act(request, signer)

    if signer.status in ("Pending", "Sent"):
        _mark_viewed(signer, request)
        request.reload()
        signer, request = _get_signer_from_token(token)
        ensure_signer_can_act(request, signer)

    profile = _get_profile_for_signer(signer)

    visible_fields = []

    for field in request.fields:
        if field.signer == signer.name:
            visible_fields.append(
                {
                    "name": field.name,
                    "field_type": field.field_type,
                    "page": field.page,
                    "x_ratio": field.x_ratio,
                    "y_ratio": field.y_ratio,
                    "width_ratio": field.width_ratio,
                    "height_ratio": field.height_ratio,
                    "required": field.required,
                    "read_only": field.read_only,
                    "default_value": field.default_value,
                }
            )

    return {
        "request": request.name,
        "title": request.request_title,
        "source_pdf": request.signed_pdf or request.source_pdf,
        "request_modified": str(request.modified),
        "consent_required": is_consent_required(),
        "signer": {
            "name": signer.name,
            "profile": signer.signer,
            "full_name": signer.full_name,
            "email": signer.email,
            "status": signer.status,
            "signing_order": signer.signing_order,
        },
        "profile": {
            "name": profile.name,
            "full_name": profile.full_name,
            "email": profile.email,
            "signature_type": profile.signature_type,
            "signature_image": profile.signature_image,
            "initials_image": profile.initials_image,
            "signature_text": profile.signature_text,
            "consent": profile.consent,
            "consent_on": profile.consent_on,
        },
        "fields": visible_fields,
    }


@frappe.whitelist(allow_guest=True)
def give_consent(token):
    signer, request = _get_signer_from_token(token)
    ensure_signer_can_act(request, signer)

    profile = _get_profile_for_signer(signer)

    profile.consent = 1

    if hasattr(profile, "consent_on"):
        profile.consent_on = now_datetime()

    profile.save(ignore_permissions=True)

    append_event(
        request.name,
        "Viewed",
        signer_email=signer.email,
        details={
            "signer": signer.name,
            "profile": profile.name,
            "consent_given": True,
            "consent_on": profile.consent_on,
        },
    )

    return {
        "status": "consented",
        "profile": profile.name,
        "consent": profile.consent,
        "consent_on": profile.consent_on,
    }


@frappe.whitelist(allow_guest=True)
def save_signing_profile_asset(token, kind, mode, data_url=None, typed_text=None):
    signer, request = _get_signer_from_token(token)
    ensure_signer_can_act(request, signer)

    profile = _get_profile_for_signer(signer)

    if kind not in ("signature", "initials"):
        frappe.throw("Invalid image type.")

    if mode not in ("draw", "upload", "type"):
        frappe.throw("Invalid signature setup mode.")

    if mode in ("draw", "upload"):
        result = attach_signature_png(
            reference_doctype="Frappe Sign Profile",
            reference_name=profile.name,
            kind=kind,
            data_url=data_url,
        )

        profile.set(result["fieldname"], result["file_url"])

        if kind == "signature":
            profile.signature_type = "Drawn" if mode == "draw" else "Uploaded"

    else:
        if not typed_text:
            frappe.throw("Please enter text.")

        png_bytes = make_typed_signature_png(typed_text, kind)

        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("utf-8")

        result = attach_signature_png(
            reference_doctype="Frappe Sign Profile",
            reference_name=profile.name,
            kind=kind,
            data_url=data_url,
        )

        profile.set(result["fieldname"], result["file_url"])

        if kind == "signature":
            profile.signature_type = "Typed"
            profile.signature_text = typed_text

    profile.save(ignore_permissions=True)

    append_event(
        request.name,
        "Viewed",
        signer_email=signer.email,
        details={
            "signer": signer.name,
            "profile": profile.name,
            "profile_asset_updated": True,
            "kind": kind,
            "mode": mode,
        },
    )

    return {
        "status": "saved",
        "kind": kind,
        "mode": mode,
        "signature_type": profile.signature_type,
        "signature_image": profile.signature_image,
        "initials_image": profile.initials_image,
        "signature_text": profile.signature_text,
        "consent": profile.consent,
        "consent_on": profile.consent_on,
    }


def make_typed_signature_png(text, kind):
    text = str(text or "").strip()

    if not text:
        frappe.throw("Please enter text.")

    width = 720 if kind == "signature" else 360
    height = 260 if kind == "signature" else 180

    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    font = get_signature_font(96 if kind == "signature" else 72)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = max((width - text_width) / 2, 10)
    y = max((height - text_height) / 2, 10)

    draw.text((x, y), text, fill=(17, 24, 39, 255), font=font)

    output = BytesIO()
    image.save(output, format="PNG")

    return output.getvalue()


def get_signature_font(size):
    candidates = [
        frappe.get_app_path("frappe_sign", "public", "fonts", "GreatVibes-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue

    return ImageFont.load_default()


@frappe.whitelist(allow_guest=True)
def get_signer_profile_image(token, kind):
    signer, request = _get_signer_from_token(token)
    ensure_signer_can_act(request, signer)

    profile = _get_profile_for_signer(signer)

    if kind == "signature":
        file_url = profile.signature_image
        filename = f"{profile.name}-signature.png"
    elif kind == "initials":
        file_url = profile.initials_image
        filename = f"{profile.name}-initials.png"
    else:
        frappe.throw("Invalid image type.")

    if not file_url:
        frappe.throw("No image is saved for this profile.")

    image_bytes = get_file_bytes(file_url)

    frappe.local.response.filename = filename
    frappe.local.response.filecontent = image_bytes
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"


@frappe.whitelist(allow_guest=True)
def complete_signing(token, field_values=None, request_modified=None):
    signer, request = _get_signer_from_token(token)

    lock_request(request.name)
    request.reload()

    signer_row = _get_signer_row(request, signer.name)

    if not signer_row:
        frappe.throw("Signer row not found.")

    ensure_signer_can_act(request, signer_row)

    if signer_row.status not in OPEN_SIGNER_STATUSES:
        frappe.throw("This signing request has already been completed or is no longer available.")

    if request.status in CLOSED_REQUEST_STATUSES:
        frappe.throw(f"This signing request is {request.status}.")

    if request_modified and str(request.modified) != str(request_modified):
        frappe.throw(
            "This signing request changed while you were signing. "
            "Please reload the page and review the latest version before completing."
        )

    profile = _get_profile_for_signer(signer_row)

    if is_consent_required() and not profile.consent:
        frappe.throw("You must give consent before signing.")

    parsed_values = parse_field_values(field_values)
    stamp_items = build_stamp_items(request, signer_row, profile, parsed_values)

    base_pdf_url = request.signed_pdf or request.source_pdf

    if not base_pdf_url:
        frappe.throw("No PDF is attached to this signing request.")

    base_pdf_bytes = get_file_bytes(base_pdf_url)
    signed_pdf_bytes = stamp_pdf_fields(base_pdf_bytes, stamp_items)
    signed_pdf_hash = sha256_bytes(signed_pdf_bytes)

    file_doc = attach_private_file(
        "Frappe Sign Request",
        request.name,
        f"{frappe.scrub(request.name)}-signed.pdf",
        signed_pdf_bytes,
    )

    request.signed_pdf = file_doc.file_url
    request.signed_pdf_hash = signed_pdf_hash

    signer_row.status = "Signed"
    signer_row.signed_on = now_datetime()
    signer_row.ip_address = getattr(frappe.local, "request_ip", None)
    signer_row.user_agent = frappe.get_request_header("User-Agent")

    request.save(ignore_permissions=True)

    create_file_hash(
        request.name,
        "Signed PDF",
        request.signed_pdf,
        signed_pdf_hash,
        "Post-Sign",
    )

    append_event(
        request.name,
        "PDF Stamped",
        signer_email=signer_row.email,
        details={
            "signer": signer_row.name,
            "profile": profile.name,
            "field_count": len(stamp_items),
            "signed_pdf": request.signed_pdf,
        },
        document_hash=signed_pdf_hash,
    )

    append_event(
        request.name,
        "Signed",
        signer_email=signer_row.email,
        details={
            "signer": signer_row.name,
            "profile": profile.name,
            "field_count": len(stamp_items),
        },
        document_hash=signed_pdf_hash,
    )

    _refresh_request_status(request)

    updated_request = frappe.get_doc("Frappe Sign Request", request.name)

    if updated_request.status != "Completed":
        send_progress_notification(updated_request, signer_row, "Signed")

    return {
        "status": signer_row.status,
        "request_status": updated_request.status,
        "signed_pdf": updated_request.signed_pdf,
        "signed_pdf_hash": signed_pdf_hash,
    }


@frappe.whitelist(allow_guest=True)
def decline_signing(token, reason):
    signer, request = _get_signer_from_token(token)

    lock_request(request.name)
    request.reload()

    signer_row = _get_signer_row(request, signer.name)

    if not signer_row:
        frappe.throw("Signer row not found.")

    ensure_signer_can_act(request, signer_row)

    if signer_row.status not in OPEN_SIGNER_STATUSES:
        frappe.throw("This signing request has already been completed or is no longer available.")

    if request.status in CLOSED_REQUEST_STATUSES:
        frappe.throw(f"This signing request is {request.status}.")

    signer_row.status = "Declined"
    signer_row.declined_on = now_datetime()
    signer_row.decline_reason = reason
    signer_row.ip_address = getattr(frappe.local, "request_ip", None)
    signer_row.user_agent = frappe.get_request_header("User-Agent")

    request.status = "Declined"
    request.declined_on = now_datetime()
    request.decline_reason = reason
    request.save(ignore_permissions=True)

    append_event(
        request.name,
        "Declined",
        signer_email=signer_row.email,
        details={
            "signer": signer_row.name,
            "reason": reason,
        },
    )

    send_progress_notification(request, signer_row, "Declined", reason=reason)

    return {"status": "Declined"}


def lock_request(request_name):
    frappe.db.sql(
        "select name from `tabFrappe Sign Request` where name = %s for update",
        request_name,
    )


def parse_field_values(field_values):
    if not field_values:
        return {}

    if isinstance(field_values, dict):
        return field_values

    try:
        return json.loads(field_values)
    except Exception:
        frappe.throw("Invalid field value payload.")


def build_stamp_items(request, signer_row, profile, field_values):
    stamp_items = []

    fields = [field for field in request.fields if field.signer == signer_row.name]

    for field in fields:
        client_value = field_values.get(field.name, {})
        value = extract_client_value(client_value)

        image_bytes = None
        stamp_value = value

        if field.field_type == "Name":
            stamp_value = signer_row.full_name or profile.full_name

        elif field.field_type == "Email":
            stamp_value = signer_row.email or profile.email

        elif field.field_type == "Date":
            stamp_value = frappe.utils.formatdate(now_datetime().date(), "MMMM d, yyyy")

        elif field.field_type == "Signature":
            if profile.signature_image:
                image_bytes = get_file_bytes(profile.signature_image)
                stamp_value = ""
            elif profile.signature_type == "Typed" and profile.signature_text:
                stamp_value = profile.signature_text
            else:
                frappe.throw("No signature image or typed signature is saved on your Frappe Sign Profile.")

        elif field.field_type == "Initials":
            if profile.initials_image:
                image_bytes = get_file_bytes(profile.initials_image)
                stamp_value = ""
            else:
                frappe.throw("No initials image is saved on your Frappe Sign Profile.")

        elif field.field_type == "Text":
            if field.required and not value:
                frappe.throw("Please complete all required text fields.")

        elif field.field_type == "Checkbox":
            if field.required and str(value) not in ("1", "true", "True", "yes", "Yes"):
                frappe.throw("Please complete all required checkbox fields.")

        if field.required and field.field_type not in ("Checkbox",) and not stamp_value and not image_bytes:
            frappe.throw("Please complete all required signing fields.")

        stamp_items.append(
            {
                "field_type": field.field_type,
                "page": field.page,
                "x_ratio": field.x_ratio,
                "y_ratio": field.y_ratio,
                "width_ratio": field.width_ratio,
                "height_ratio": field.height_ratio,
                "value": stamp_value,
                "image_bytes": image_bytes,
            }
        )

    return stamp_items


def extract_client_value(client_value):
    if isinstance(client_value, dict):
        return client_value.get("value")

    return client_value


def _mark_viewed(signer, request):
    _update_signer_row(
        request,
        signer.name,
        {
            "status": "Viewed",
            "viewed_on": now_datetime(),
            "ip_address": getattr(frappe.local, "request_ip", None),
            "user_agent": frappe.get_request_header("User-Agent"),
        },
    )

    if request.status in ("Prepared", "Sent"):
        request.status = "Viewed"
        request.save(ignore_permissions=True)

    append_event(
        request.name,
        "Viewed",
        signer_email=signer.email,
        details={"signer": signer.name},
    )


def _update_signer_row(request, signer_row_name, values):
    for row in request.signers:
        if row.name == signer_row_name:
            for key, value in values.items():
                row.set(key, value)

            request.save(ignore_permissions=True)
            return

    frappe.throw("Signer row not found.")


def _refresh_request_status(request):
    request.reload()

    signer_rows = [row for row in request.signers if row.role == "Signer"]

    if signer_rows and all(row.status == "Signed" for row in signer_rows):
        complete_request_with_audit_certificate(request)
        return

    if request.signing_mode == "Sequential":
        advance_sequential_order(request)

    request.status = "Partially Signed"
    request.save(ignore_permissions=True)


def complete_request_with_audit_certificate(request):
    settings = get_settings()

    request.status = "Completed"
    request.completed_on = now_datetime()
    request.save(ignore_permissions=True)

    append_event(
        request.name,
        "Completed",
        details={
            "signed_pdf": request.signed_pdf,
            "signed_pdf_hash": request.signed_pdf_hash,
            "completed_on": request.completed_on,
        },
        document_hash=request.signed_pdf_hash,
    )

    final_certificate = generate_audit_certificate(request.name, suffix="final")

    request.reload()
    request.audit_certificate = final_certificate["file_url"]
    request.save(ignore_permissions=True)

    create_file_hash(
        request.name,
        "Audit Certificate",
        request.audit_certificate,
        final_certificate["sha256_hash"],
        "Certificate",
    )

    append_event(
        request.name,
        "Certificate Generated",
        details={
            "audit_certificate": request.audit_certificate,
            "certificate_hash": final_certificate["sha256_hash"],
            "final_certificate": True,
        },
        document_hash=final_certificate["sha256_hash"],
    )

    request.reload()

    create_audit_certificate_record(
        request.name,
        audit_certificate_hash=final_certificate["sha256_hash"],
    )

    audit_appended = False
    digitally_signed = False

    if settings.append_audit_certificate:
        apply_audit_certificate_to_signed_pdf(request.name)
        audit_appended = True

    if settings.enable_certificate_based_pdf_signing:
        apply_digital_signature_to_final_pdf(request.name)
        digitally_signed = True

    request.reload()

    if request.certificate_signed_pdf:
        create_final_verification_package_record(
            request.name,
            audit_appended=audit_appended,
            digitally_signed=digitally_signed,
        )

    send_completion_notification(request)

    submit_completed_request(request.name)

    request.reload()
    apply_source_document_actions(request)


def apply_source_document_actions(request):
    """Attaches the final signed PDF back onto the source document (and
    submits it) once a request has completed, per its
    attach_signed_pdf_to_source/attach_signed_pdf_field/
    submit_source_on_completion fields (each snapshotted from Frappe Sign
    Settings' per-DocType configuration at request-creation time, see
    create_from_source() in api/request.py).

    Best-effort: a problem with the source document (deleted since, an
    Attach field that rejects the value, a submit validation failure, etc.)
    must not undo the signing request's own already-successful completion,
    so failures here are logged rather than raised.
    """
    if request.source_type != "Frappe Document":
        return

    if not request.source_doctype or not request.source_name:
        return

    if not frappe.db.exists(request.source_doctype, request.source_name):
        return

    try:
        if request.attach_signed_pdf_to_source:
            final_pdf_url = request.certificate_signed_pdf or request.signed_pdf

            if final_pdf_url:
                pdf_bytes = get_file_bytes(final_pdf_url)
                file_doc = attach_private_file(
                    request.source_doctype,
                    request.source_name,
                    f"{frappe.scrub(request.source_doctype)}-{request.source_name}-signed.pdf",
                    pdf_bytes,
                )

                if request.attach_signed_pdf_field:
                    frappe.db.set_value(
                        request.source_doctype,
                        request.source_name,
                        request.attach_signed_pdf_field,
                        file_doc.file_url,
                        update_modified=False,
                    )

                    # Tag the File itself as belonging to that field too -
                    # otherwise Frappe's own attach_files_to_document()
                    # on_update hook (frappe/core/doctype/file/utils.py)
                    # won't recognize this File as already attached to the
                    # field and will create a second, duplicate File the
                    # next time the source document is saved/submitted.
                    frappe.db.set_value(
                        "File",
                        file_doc.name,
                        "attached_to_field",
                        request.attach_signed_pdf_field,
                        update_modified=False,
                    )

                append_event(
                    request.name,
                    "Signed PDF Attached To Source",
                    details={
                        "source_doctype": request.source_doctype,
                        "source_name": request.source_name,
                        "file_url": file_doc.file_url,
                        "attach_signed_pdf_field": request.attach_signed_pdf_field,
                    },
                )

        if request.submit_source_on_completion:
            source_doc = frappe.get_doc(request.source_doctype, request.source_name)

            if source_doc.meta.is_submittable and source_doc.docstatus == 0:
                source_doc.flags.ignore_permissions = True
                source_doc.submit()

                append_event(
                    request.name,
                    "Source Document Submitted",
                    details={
                        "source_doctype": request.source_doctype,
                        "source_name": request.source_name,
                    },
                )
    except Exception:
        frappe.log_error(
            title="Frappe Sign: source document action failed",
            message=frappe.get_traceback(),
        )


def apply_audit_certificate_to_signed_pdf(request_name):
    request = frappe.get_doc("Frappe Sign Request", request_name)

    if not request.signed_pdf:
        frappe.throw("Cannot create audit-appended PDF without a signed PDF.")

    if not request.audit_certificate:
        frappe.throw("Cannot create audit-appended PDF without an audit certificate.")

    signed_pdf_bytes = get_file_bytes(request.signed_pdf)
    certificate_bytes = get_file_bytes(request.audit_certificate)

    audit_appended_pdf_bytes = append_pdf_bytes(
        signed_pdf_bytes,
        certificate_bytes,
    )

    audit_appended_pdf_hash = sha256_bytes(audit_appended_pdf_bytes)

    file_doc = attach_private_file(
        "Frappe Sign Request",
        request.name,
        f"{frappe.scrub(request.name)}-audit-appended.pdf",
        audit_appended_pdf_bytes,
    )

    request.certificate_signed_pdf = file_doc.file_url
    request.certificate_signed_pdf_hash = audit_appended_pdf_hash
    request.save(ignore_permissions=True)

    create_file_hash(
        request.name,
        "Audit Appended PDF",
        request.certificate_signed_pdf,
        audit_appended_pdf_hash,
        "Verification",
    )

    append_event(
        request.name,
        "Certificate Applied",
        details={
            "operation": "audit_certificate_appended",
            "signed_pdf": request.signed_pdf,
            "audit_certificate": request.audit_certificate,
            "audit_appended_pdf": request.certificate_signed_pdf,
            "audit_appended_pdf_hash": audit_appended_pdf_hash,
        },
        document_hash=audit_appended_pdf_hash,
    )

    return {
        "certificate_signed_pdf": request.certificate_signed_pdf,
        "certificate_signed_pdf_hash": audit_appended_pdf_hash,
    }


def apply_digital_signature_to_final_pdf(request_name):
    request = frappe.get_doc("Frappe Sign Request", request_name)
    settings = get_settings()

    if not settings.certificate_file:
        frappe.throw(
            "Certificate-based PDF signing is enabled, but no certificate file is configured "
            "in Frappe Sign Settings."
        )

    password = get_certificate_password(settings)

    base_pdf_url = request.certificate_signed_pdf or request.signed_pdf

    if not base_pdf_url:
        frappe.throw("Cannot digitally sign PDF because no final PDF is available.")

    base_pdf_bytes = get_file_bytes(base_pdf_url)
    certificate_bytes = get_file_bytes(settings.certificate_file)

    digitally_signed_pdf_bytes = digitally_sign_pdf(
        pdf_bytes=base_pdf_bytes,
        certificate_bytes=certificate_bytes,
        password=password,
        reason="Frappe Sign certificate-based PDF signing",
        location=settings.certificate_location,
        field_name="FrappeSignDigitalSignature",
    )

    digitally_signed_pdf_hash = sha256_bytes(digitally_signed_pdf_bytes)

    file_doc = attach_private_file(
        "Frappe Sign Request",
        request.name,
        f"{frappe.scrub(request.name)}-digitally-signed.pdf",
        digitally_signed_pdf_bytes,
    )

    request.certificate_signed_pdf = file_doc.file_url
    request.certificate_signed_pdf_hash = digitally_signed_pdf_hash
    request.save(ignore_permissions=True)

    create_file_hash(
        request.name,
        "Digitally Signed PDF",
        request.certificate_signed_pdf,
        digitally_signed_pdf_hash,
        "Verification",
    )

    append_event(
        request.name,
        "Certificate Applied",
        details={
            "operation": "digital_signature_applied",
            "base_pdf": base_pdf_url,
            "digitally_signed_pdf": request.certificate_signed_pdf,
            "digitally_signed_pdf_hash": digitally_signed_pdf_hash,
            "certificate_file": settings.certificate_file,
            "certificate_location": settings.certificate_location,
        },
        document_hash=digitally_signed_pdf_hash,
    )

    return {
        "certificate_signed_pdf": request.certificate_signed_pdf,
        "certificate_signed_pdf_hash": digitally_signed_pdf_hash,
    }


def get_certificate_password(settings):
    if not settings.certificate_password:
        return None

    try:
        return settings.get_password("certificate_password")
    except Exception:
        return settings.certificate_password


def submit_completed_request(request_name):
    request = frappe.get_doc("Frappe Sign Request", request_name)

    if request.docstatus != 0:
        return

    if request.status != "Completed":
        return

    if not request.signed_pdf:
        frappe.throw("Cannot submit a completed signing request without a signed PDF.")

    if not request.audit_certificate:
        frappe.throw("Cannot submit a completed signing request without an audit certificate.")

    request.flags.ignore_permissions = True
    request.submit()


def advance_sequential_order(request):
    signer_rows = [row for row in request.signers if row.role == "Signer"]

    open_rows = [
        row
        for row in signer_rows
        if row.status in OPEN_SIGNER_STATUSES
    ]

    if not open_rows:
        return

    current_order = request.current_signing_order or 1

    current_order_open_rows = [
        row
        for row in open_rows
        if (row.signing_order or 1) == current_order
    ]

    if current_order_open_rows:
        return

    next_orders = sorted(
        {
            row.signing_order or 1
            for row in open_rows
            if (row.signing_order or 1) > current_order
        }
    )

    if next_orders:
        request.current_signing_order = next_orders[0]


@frappe.whitelist(allow_guest=True)
def get_source_pdf(token):
    signer, request = _get_signer_from_token(token)
    ensure_signer_can_act(request, signer)

    pdf_url = request.signed_pdf or request.source_pdf

    if not pdf_url:
        frappe.throw("No source PDF is attached to this signing request.")

    pdf_bytes = get_file_bytes(pdf_url)

    frappe.local.response.filename = f"{request.name}.pdf"
    frappe.local.response.filecontent = pdf_bytes
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"