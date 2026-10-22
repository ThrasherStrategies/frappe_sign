# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.utils import cint

from frappe_sign.api.request import create_from_source, send_request
from frappe_sign.permissions import is_frappe_sign_sender


VALID_SIGNER_ROLES = {
	"Signer",
	"Viewer",
	"Approver",
}

VALID_FIELD_TYPES = {
	"Signature",
	"Initials",
	"Name",
	"Email",
	"Date",
	"Text",
	"Checkbox",
}

VALID_SIGNING_MODES = {
	"Parallel",
	"Sequential",
}


@frappe.whitelist()
def create_signing_request_from_document(
	source_doctype,
	source_name,
	print_format=None,
	request_title=None,
	signing_mode="Parallel",
	signers=None,
	fields=None,
	send=False,
):
	"""
	Create a Frappe Sign Request from another Frappe document.

	This is the recommended integration API for other Frappe apps.

	Expected signers payload:
	[
		{
			"key": "employee",
			"profile": "FSP-0001",
			"role": "Signer",
			"signing_order": 1
		}
	]

	Expected fields payload:
	[
		{
			"signer_key": "employee",
			"field_type": "Signature",
			"page": 1,
			"x_ratio": 0.12,
			"y_ratio": 0.82,
			"width_ratio": 0.28,
			"height_ratio": 0.06,
			"required": 1
		}
	]

	The field signer may be referenced by:
	- signer_key
	- signer_ref
	- profile
	- signer

	Frappe Sign Field.signer is always mapped to the request-specific
	Frappe Sign Signer child row name before saving.
	"""

	if not is_frappe_sign_sender():
		frappe.throw("You do not have permission to create Frappe Sign requests.")

	source_doctype = require_value(source_doctype, "Source DocType is required.")
	source_name = require_value(source_name, "Source Name is required.")

	if signing_mode not in VALID_SIGNING_MODES:
		frappe.throw("Signing Mode must be Parallel or Sequential.")

	parsed_signers = parse_json_payload(signers, "signers")
	parsed_fields = parse_json_payload(fields, "fields")

	if not parsed_signers:
		frappe.throw("At least one signer is required.")

	if not parsed_fields:
		frappe.throw("At least one signing field is required.")

	result = create_from_source(
		doctype=source_doctype,
		name=source_name,
		print_format=print_format,
	)

	request = frappe.get_doc("Frappe Sign Request", result["name"])

	if request_title:
		request.request_title = request_title

	request.signing_mode = signing_mode

	if signing_mode == "Sequential":
		request.current_signing_order = 1
	else:
		request.current_signing_order = 1

	profile_to_signer = add_signers_to_request(request, parsed_signers, signing_mode)
	add_fields_to_request(request, parsed_fields, profile_to_signer)

	request.save(ignore_permissions=True)

	response = {
		"name": request.name,
		"status": request.status,
		"source_pdf": request.source_pdf,
		"signer_count": len(request.signers or []),
		"field_count": len(request.fields or []),
		"sent": False,
	}

	if truthy(send):
		send_result = send_request(request.name)
		response.update(
			{
				"status": send_result.get("status"),
				"sent": True,
				"sent_signer_count": send_result.get("signer_count"),
			}
		)

	return response


def add_signers_to_request(request, signers, signing_mode):
	request.set("signers", [])

	profile_to_signer = {}

	for index, signer_payload in enumerate(signers, start=1):
		if not isinstance(signer_payload, dict):
			frappe.throw("Each signer must be an object.")

		profile_name = resolve_profile_name(signer_payload)
		profile = get_active_profile(profile_name)

		role = signer_payload.get("role") or "Signer"

		if role not in VALID_SIGNER_ROLES:
			frappe.throw(f"Invalid signer role: {role}")

		signing_order = get_signing_order(signer_payload, index, signing_mode)

		row = request.append(
			"signers",
			{
				"signer": profile.name,
				"full_name": profile.full_name,
				"email": profile.email,
				"role": role,
				"signing_order": signing_order,
				"status": "Pending",
			},
		)
		# Frappe 15 names child rows only on save; fields link to the row by name.
		if not row.name:
			row.name = frappe.generate_hash(length=10)

		keys = get_signer_reference_keys(signer_payload, profile.name)

		for key in keys:
			profile_to_signer[str(key)] = row

	return profile_to_signer


def add_fields_to_request(request, fields, profile_to_signer):
	request.set("fields", [])

	for field_payload in fields:
		if not isinstance(field_payload, dict):
			frappe.throw("Each signing field must be an object.")

		field_type = require_value(field_payload.get("field_type"), "Field Type is required.")

		if field_type not in VALID_FIELD_TYPES:
			frappe.throw(f"Invalid signing field type: {field_type}")

		signer_row = resolve_field_signer(field_payload, profile_to_signer, field_type)

		page = cint(require_value(field_payload.get("page"), "Page is required."))

		if page < 1:
			frappe.throw("Page must be 1 or greater.")

		x_ratio = require_ratio(field_payload.get("x_ratio"), "x_ratio")
		y_ratio = require_ratio(field_payload.get("y_ratio"), "y_ratio")
		width_ratio = require_ratio(field_payload.get("width_ratio"), "width_ratio")
		height_ratio = require_ratio(field_payload.get("height_ratio"), "height_ratio")

		request.append(
			"fields",
			{
				"signer": signer_row.name if signer_row else None,
				"field_type": field_type,
				"page": page,
				"x_ratio": x_ratio,
				"y_ratio": y_ratio,
				"width_ratio": width_ratio,
				"height_ratio": height_ratio,
				"required": 1 if truthy(field_payload.get("required", 1)) else 0,
				"read_only": 1 if truthy(field_payload.get("read_only", 0)) else 0,
				"default_value": field_payload.get("default_value") or "",
			},
		)


def resolve_profile_name(signer_payload):
	profile_name = (
		signer_payload.get("profile")
		or signer_payload.get("signer")
		or signer_payload.get("profile_name")
	)

	if profile_name:
		return profile_name

	user = signer_payload.get("user")
	email = signer_payload.get("email")

	if user:
		profile_name = frappe.db.get_value(
			"Frappe Sign Profile",
			{
				"user": user,
				"active": 1,
			},
			"name",
		)

		if profile_name:
			return profile_name

	if email:
		profile_name = frappe.db.get_value(
			"Frappe Sign Profile",
			{
				"email": email,
				"active": 1,
			},
			"name",
		)

		if profile_name:
			return profile_name

	frappe.throw("Each signer must reference an active Frappe Sign Profile, User, or Email.")


def get_active_profile(profile_name):
	profile = frappe.db.get_value(
		"Frappe Sign Profile",
		{
			"name": profile_name,
			"active": 1,
		},
		[
			"name",
			"full_name",
			"email",
			"user",
			"active",
		],
		as_dict=True,
	)

	if not profile:
		frappe.throw(f"Frappe Sign Profile is not active or does not exist: {profile_name}")

	if not profile.full_name:
		frappe.throw(f"Frappe Sign Profile is missing Full Name: {profile_name}")

	if not profile.email:
		frappe.throw(f"Frappe Sign Profile is missing Email: {profile_name}")

	return profile


def get_signing_order(signer_payload, index, signing_mode):
	if signing_mode != "Sequential":
		return 1

	signing_order = signer_payload.get("signing_order")

	if signing_order in (None, ""):
		signing_order = index

	signing_order = cint(signing_order)

	if signing_order < 1:
		frappe.throw("Signing Order must be 1 or greater.")

	return signing_order


def get_signer_reference_keys(signer_payload, profile_name):
	keys = {
		profile_name,
		signer_payload.get("key"),
		signer_payload.get("ref"),
		signer_payload.get("signer_key"),
		signer_payload.get("signer_ref"),
		signer_payload.get("profile"),
		signer_payload.get("signer"),
		signer_payload.get("profile_name"),
		signer_payload.get("user"),
		signer_payload.get("email"),
	}

	return [key for key in keys if key]


def resolve_field_signer(field_payload, profile_to_signer, field_type):
	if field_type not in ("Signature", "Initials", "Name", "Email", "Date", "Text", "Checkbox"):
		return None

	reference = (
		field_payload.get("signer_key")
		or field_payload.get("signer_ref")
		or field_payload.get("profile")
		or field_payload.get("signer")
		or field_payload.get("profile_name")
		or field_payload.get("user")
		or field_payload.get("email")
	)

	if not reference:
		frappe.throw(f"{field_type} field requires a signer reference.")

	signer_row = profile_to_signer.get(str(reference))

	if not signer_row:
		frappe.throw(f"Could not match field signer reference to a signer row: {reference}")

	return signer_row


def parse_json_payload(value, label):
	if value in (None, ""):
		return []

	if isinstance(value, str):
		try:
			return json.loads(value)
		except Exception:
			frappe.throw(f"Invalid JSON payload for {label}.")

	if isinstance(value, list):
		return value

	frappe.throw(f"{label} must be a JSON string or list.")


def require_value(value, message):
	if value in (None, ""):
		frappe.throw(message)

	return value


def require_ratio(value, fieldname):
	if value in (None, ""):
		frappe.throw(f"{fieldname} is required.")

	try:
		value = float(value)
	except Exception:
		frappe.throw(f"{fieldname} must be a number.")

	if value < 0 or value > 1:
		frappe.throw(f"{fieldname} must be between 0 and 1.")

	return value


def truthy(value):
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "y", "on")

	return bool(value)