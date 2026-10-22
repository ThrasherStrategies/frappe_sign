app_name = "frappe_sign"
app_title = "Frappe Sign"
app_publisher = "BuFf0k"
app_description = "An integrated e-Signature Application"
app_email = "buff0k@gmail.com"
app_license = "mit"
add_to_apps_screen = [
    {
        "name": "frappe_sign",
        "logo": "/assets/frappe_sign/desktop_icons/frappe_sign_icon.png",
        "title": "Frappe Sign",
        "route": "/app/frappe-sign-sign",  # Frappe 15 desk route (v16: /desk/...)
        "has_permission": "frappe_sign.permissions.has_app_permission",
    }
]
fixtures = [
    {"dt": "Role", "filters": [["role_name", "in", [
        "Frappe Sign User",
        "Frappe Sign Sender",
        "Frappe Sign Manager",
    ]]]}
]
permission_query_conditions = {
    "Frappe Sign Request": "frappe_sign.permissions.frappe_sign_request_query",
    "Frappe Sign Event": "frappe_sign.permissions.frappe_sign_event_query",
    "Frappe Sign Profile": "frappe_sign.permissions.frappe_sign_profile_query",
    "Frappe Sign Certificate": "frappe_sign.permissions.frappe_sign_certificate_query",
    "Frappe Sign File Hash": "frappe_sign.permissions.frappe_sign_file_hash_query",
}
has_permission = {
    "Frappe Sign Request": "frappe_sign.permissions.has_frappe_sign_request_permission",
    "Frappe Sign Event": "frappe_sign.permissions.has_frappe_sign_event_permission",
    "Frappe Sign Profile": "frappe_sign.permissions.has_frappe_sign_profile_permission",
    "Frappe Sign Certificate": "frappe_sign.permissions.has_frappe_sign_certificate_permission",
    "Frappe Sign File Hash": "frappe_sign.permissions.has_frappe_sign_file_hash_permission",
    # Primary defense against deleting/replacing a signing request's PDF out
    # from under it - see utils/file_lock.py for why this (not a doc_events
    # hook) is the layer that actually has to do the work.
    "File": "frappe_sign.utils.file_lock.has_locked_file_permission",
}
doc_events = {
    "File": {
        # Secondary net for Administrator/ignore_permissions callers, who
        # bypass has_permission entirely - see utils/file_lock.py.
        "validate": "frappe_sign.utils.file_lock.guard_file_changes",
        "on_trash": "frappe_sign.utils.file_lock.guard_file_delete",
    },
}
website_route_rules = [
    {"from_route": "/sign/<token>", "to_route": "frappe_sign_portal"},
    {"from_route": "/frappe-sign-portal", "to_route": "frappe_sign_portal"},
    {"from_route": "/frappe-sign-profile", "to_route": "frappe_sign_profile"},
    {"from_route": "/frappe-sign-documents", "to_route": "frappe_sign_documents"},
]
app_include_css = [
    "/assets/frappe_sign/css/frappe_sign.css",
]
app_include_js = [
    "/assets/frappe_sign/js/frappe_sign_button.js",
]
standard_portal_menu_items = [
	{
		"title": "User Signature Profile",
        "route": "/frappe-sign-profile",
        "reference_doctype": "Frappe Sign Profile",
        "role": "Frappe Sign User",
	},
    {
        "title": "Documents To Sign",
        "route": "/frappe-sign-documents",
        "reference_doctype": "Frappe Sign Request",
    },
]
scheduler_events = {
    "daily": [
        "frappe_sign.utils.notifications.send_daily_signing_reminders",
        "frappe_sign.utils.tamper.run_scheduled_tamper_checks",
    ],
}