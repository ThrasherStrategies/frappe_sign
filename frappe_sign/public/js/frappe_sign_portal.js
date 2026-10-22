// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

class FrappeSignPortal {
    constructor(root) {
        this.root = $(root);
        this.token = this.root.attr("data-token");
        this.request_name = this.root.attr("data-request");

        this.context = null;
        this.pdf_doc = null;
        this.scale = 1.2;

        this.setup_kind = null;
        this.setup_mode = "draw";

        this.canvas = null;
        this.ctx = null;
        this.drawing = false;
        this.last_point = null;

        this.init();
    }

    async init() {
        if (!this.token) {
            this.show_error("Missing signing token.");
            return;
        }

        if (!window.pdfjsLib) {
            this.show_error("PDF.js failed to load.");
            return;
        }

        this.bind_events();
        this.add_signing_modals();

        await this.load_context();
        await this.load_pdf();
        await this.render_pdf();

        await this.start_required_profile_flow();
    }

    bind_events() {
        $("#frappe-sign-complete").on("click", async () => {
            await this.complete_signing();
        });

        $("#frappe-sign-decline").on("click", async () => {
            await this.decline_signing();
        });

        $("#frappe-sign-go-next").on("click", () => {
            this.go_to_next_required_field();
        });

        $("#frappe-sign-sign-all").on("click", () => {
            this.sign_all_available_fields();
        });
    }

    add_signing_modals() {
        if ($("#frappe-sign-consent-modal").length) {
            return;
        }

        $("body").append(`
            <div class="frappe-sign-modal-backdrop" id="frappe-sign-consent-modal" hidden>
                <div class="frappe-sign-modal">
                    <div class="frappe-sign-modal-header">
                        <h3>${__("Consent Required")}</h3>
                    </div>

                    <p>
                        ${__("Before signing, you must consent to using your saved signature and initials for electronic signing in Frappe Sign.")}
                    </p>

                    <div class="frappe-sign-modal-footer">
                        <button type="button" class="btn btn-primary" id="frappe-sign-consent-confirm">
                            ${__("I Consent")}
                        </button>
                    </div>
                </div>
            </div>

            <div class="frappe-sign-modal-backdrop" id="frappe-sign-setup-modal" hidden>
                <div class="frappe-sign-modal">
                    <div class="frappe-sign-modal-header">
                        <h3 id="frappe-sign-setup-title">${__("Set Up Signature")}</h3>
                        <button type="button" class="btn btn-default btn-sm" id="frappe-sign-setup-close">
                            ${__("Close")}
                        </button>
                    </div>

                    <p id="frappe-sign-setup-help" class="frappe-sign-muted"></p>

                    <div class="frappe-sign-setup-tabs">
                        <button type="button" class="btn btn-primary btn-sm" data-mode="draw">
                            ${__("Draw")}
                        </button>
                        <button type="button" class="btn btn-default btn-sm" data-mode="upload">
                            ${__("Upload PNG")}
                        </button>
                        <button type="button" class="btn btn-default btn-sm" data-mode="type">
                            ${__("Type")}
                        </button>
                    </div>

                    <div class="frappe-sign-setup-pane" data-pane="draw">
                        <div class="frappe-sign-draw-pad">
                            <div class="frappe-sign-draw-pad-toolbar">
                                <button type="button" class="btn btn-default btn-sm" id="frappe-sign-setup-clear">
                                    ${__("Clear")}
                                </button>
                            </div>

                            <canvas class="frappe-sign-draw-canvas" id="frappe-sign-setup-canvas"></canvas>

                            <p class="frappe-sign-draw-pad-note">
                                ${__("The saved image will be stored as a transparent PNG.")}
                            </p>
                        </div>
                    </div>

                    <div class="frappe-sign-setup-pane" data-pane="upload" hidden>
                        <input type="file" class="form-control" id="frappe-sign-setup-file" accept="image/png">
                        <p class="frappe-sign-help">${__("Only PNG files are supported.")}</p>
                    </div>

                    <div class="frappe-sign-setup-pane" data-pane="type" hidden>
                        <input type="text" class="form-control" id="frappe-sign-setup-text" placeholder="${__("Type here")}">
                    </div>

                    <div class="frappe-sign-modal-footer">
                        <button type="button" class="btn btn-primary" id="frappe-sign-setup-save">
                            ${__("Save")}
                        </button>
                    </div>
                </div>
            </div>
        `);

        $("#frappe-sign-consent-confirm").on("click", async () => {
            await this.give_consent();
            this.close_modal("#frappe-sign-consent-modal");
            await this.start_required_profile_flow();
        });

        $("#frappe-sign-setup-close").on("click", () => {
            this.close_modal("#frappe-sign-setup-modal");
        });

        $("#frappe-sign-setup-clear").on("click", () => {
            this.clear_canvas();
        });

        $("#frappe-sign-setup-save").on("click", async () => {
            await this.save_setup_asset();
        });

        $(".frappe-sign-setup-tabs button").on("click", (event) => {
            this.set_setup_mode($(event.currentTarget).attr("data-mode"));
        });

        this.initialise_canvas_once();
    }

    async load_context() {
        const response = await frappe.call({
            method: "frappe_sign.api.signing.get_signing_context",
            args: {
                token: this.token,
            },
            freeze: true,
            freeze_message: __("Loading signing request..."),
        });

        this.context = response.message;
    }

    async load_pdf() {
        const pdf_url =
            `/api/method/frappe_sign.api.signing.get_source_pdf?token=${encodeURIComponent(this.token)}`;

        this.pdf_doc = await window.pdfjsLib.getDocument(pdf_url).promise;
    }

    async render_pdf() {
        const container = $("#frappe-sign-pdf-container");
        container.empty();

        for (let page_number = 1; page_number <= this.pdf_doc.numPages; page_number++) {
            await this.render_page(page_number, container);
        }

        this.render_fields();
    }

    async render_page(page_number, container) {
        const page = await this.pdf_doc.getPage(page_number);
        // Fit the page to the screen width (phones), never larger than this.scale;
        // draw at device pixel ratio so text stays sharp.
        const pad = window.innerWidth <= 640 ? 14 : 36;
        const available = Math.max((container[0] && container[0].clientWidth) || window.innerWidth, 280) - pad;
        const fit = Math.min(this.scale, available / page.getViewport({ scale: 1 }).width);
        const viewport = page.getViewport({ scale: fit });
        const ratio = window.devicePixelRatio || 1;
        const render_viewport = page.getViewport({ scale: fit * ratio });

        const wrapper = $(`
            <div class="frappe-sign-pdf-page-wrapper" id="frappe-sign-page-${page_number}" data-page="${page_number}">
                <div class="frappe-sign-page-label">Page ${page_number}</div>
                <div class="frappe-sign-pdf-page" data-page="${page_number}">
                    <canvas class="frappe-sign-pdf-canvas"></canvas>
                    <div class="frappe-sign-page-overlay" data-page="${page_number}"></div>
                </div>
            </div>
        `);

        const canvas = wrapper.find("canvas")[0];
        const canvas_context = canvas.getContext("2d");

        canvas.width = render_viewport.width;
        canvas.height = render_viewport.height;
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;

        wrapper.find(".frappe-sign-pdf-page").css({
            width: `${viewport.width}px`,
            height: `${viewport.height}px`,
        });

        wrapper.find(".frappe-sign-page-overlay").css({
            width: `${viewport.width}px`,
            height: `${viewport.height}px`,
        });

        container.append(wrapper);

        await page.render({
            canvasContext: canvas_context,
            viewport: render_viewport,
        }).promise;
    }

    async refresh_context() {
        const response = await frappe.call({
            method: "frappe_sign.api.signing.get_signing_context",
            args: {
                token: this.token,
            },
            freeze: false,
        });

        this.context = response.message;
    }

    render_fields() {
        const fields = this.context.fields || [];

        for (const field of fields) {
            const overlay = $(`.frappe-sign-page-overlay[data-page="${field.page}"]`);

            if (!overlay.length) {
                continue;
            }

            const width = overlay.width();
            const height = overlay.height();

            const box = $(`
                <div
                    class="frappe-sign-signing-field"
                    data-field="${frappe.utils.escape_html(field.name)}"
                    data-field-type="${frappe.utils.escape_html(field.field_type)}"
                    data-required="${field.required ? "1" : "0"}"
                >
                    <div class="frappe-sign-signing-field-label">
                        ${frappe.utils.escape_html(field.field_type)}
                    </div>
                    <div class="frappe-sign-signing-field-value"></div>
                </div>
            `);

            box.css({
                left: `${field.x_ratio * width}px`,
                top: `${field.y_ratio * height}px`,
                width: `${field.width_ratio * width}px`,
                height: `${field.height_ratio * height}px`,
            });

            this.apply_field_behaviour(box, field);

            overlay.append(box);
        }
    }

    apply_field_behaviour(box, field) {
        if (field.field_type === "Name") {
            box.find(".frappe-sign-signing-field-value").text(this.context.signer.full_name || "");
            box.addClass("completed");
            return;
        }

        if (field.field_type === "Email") {
            box.find(".frappe-sign-signing-field-value").text(this.context.signer.email || "");
            box.addClass("completed");
            return;
        }

        if (field.field_type === "Date") {
            box.find(".frappe-sign-signing-field-value").text(frappe.datetime.get_today());
            box.addClass("completed");
            return;
        }

        if (field.field_type === "Text") {
            const input = $(`<input type="text" class="form-control frappe-sign-inline-input">`);
            box.find(".frappe-sign-signing-field-value").append(input);

            input.on("input", () => {
                box.toggleClass("completed", !!input.val());
            });

            return;
        }

        if (field.field_type === "Checkbox") {
            const input = $(`<input type="checkbox" class="frappe-sign-inline-checkbox">`);
            box.find(".frappe-sign-signing-field-value").append(input);

            input.on("change", () => {
                box.toggleClass("completed", input.is(":checked"));
            });

            return;
        }

        if (field.field_type === "Signature") {
            box.addClass("clickable");
            box.on("click", () => this.apply_signature_field(box));
            return;
        }

        if (field.field_type === "Initials") {
            box.addClass("clickable");
            box.on("click", () => this.apply_initials_field(box));
        }
    }

    async start_required_profile_flow() {
        if (!this.context.profile.consent) {
            this.open_modal("#frappe-sign-consent-modal");
            return;
        }

        const required = this.get_required_profile_assets();

        if (required.signature && !this.has_signature_available()) {
            this.open_setup_modal("signature");
            return;
        }

        if (required.initials && !this.has_initials_available()) {
            this.open_setup_modal("initials");
        }
    }

    get_required_profile_assets() {
        const required = {
            signature: false,
            initials: false,
        };

        for (const field of this.context.fields || []) {
            if (field.field_type === "Signature") {
                required.signature = true;
            }

            if (field.field_type === "Initials") {
                required.initials = true;
            }
        }

        return required;
    }

    has_signature_available() {
        const profile = this.context.profile || {};
        return !!profile.signature_image || !!(profile.signature_type === "Typed" && profile.signature_text);
    }

    has_initials_available() {
        const profile = this.context.profile || {};
        return !!profile.initials_image;
    }

    apply_signature_field(box) {
        const profile = this.context.profile || {};
        const value = box.find(".frappe-sign-signing-field-value");

        value.empty();

        if (profile.signature_image) {
            value.append(`
                <img
                    class="frappe-sign-signature-image"
                    src="/api/method/frappe_sign.api.signing.get_signer_profile_image?token=${encodeURIComponent(this.token)}&kind=signature"
                    alt="${__("Signature")}"
                >
            `);

            box.addClass("completed");
            return;
        }

        if (profile.signature_type === "Typed" && profile.signature_text) {
            value.append(`
                <span class="frappe-sign-typed-signature">
                    ${frappe.utils.escape_html(profile.signature_text)}
                </span>
            `);

            box.addClass("completed");
            return;
        }

        this.open_setup_modal("signature");
    }

    apply_initials_field(box) {
        const profile = this.context.profile || {};
        const value = box.find(".frappe-sign-signing-field-value");

        value.empty();

        if (profile.initials_image) {
            value.append(`
                <img
                    class="frappe-sign-initials-image"
                    src="/api/method/frappe_sign.api.signing.get_signer_profile_image?token=${encodeURIComponent(this.token)}&kind=initials"
                    alt="${__("Initials")}"
                >
            `);

            box.addClass("completed");
            return;
        }

        this.open_setup_modal("initials");
    }

    sign_all_available_fields() {
        $(".frappe-sign-signing-field").each((index, element) => {
            const box = $(element);
            const field_type = box.attr("data-field-type");

            if (box.hasClass("completed")) {
                return;
            }

            if (field_type === "Signature") {
                this.apply_signature_field(box);
            }

            if (field_type === "Initials") {
                this.apply_initials_field(box);
            }
        });

        this.go_to_next_required_field();
    }

    go_to_next_required_field() {
        const target = $(".frappe-sign-signing-field").filter(function () {
            const box = $(this);
            const required = box.attr("data-required") === "1";

            if (!required) {
                return false;
            }

            if (box.hasClass("completed")) {
                return false;
            }

            const input = box.find("input");

            if (input.length) {
                if (input.attr("type") === "checkbox") {
                    return !input.is(":checked");
                }

                return !input.val();
            }

            return true;
        }).first();

        if (!target.length) {
            frappe.show_alert({
                message: __("All required fields are complete."),
                indicator: "green",
            });
            return;
        }

        target[0].scrollIntoView({
            behavior: "smooth",
            block: "center",
        });

        target.addClass("frappe-sign-field-attention");

        setTimeout(() => {
            target.removeClass("frappe-sign-field-attention");
        }, 1400);
    }

    open_setup_modal(kind) {
        this.setup_kind = kind;
        this.set_setup_mode("draw");

        const title = kind === "signature" ? __("Set Up Signature") : __("Set Up Initials");
        const help = kind === "signature"
            ? __("You need a saved signature before completing this document.")
            : __("You need saved initials before completing this document.");

        $("#frappe-sign-setup-title").text(title);
        $("#frappe-sign-setup-help").text(help);
        $("#frappe-sign-setup-text").val("");
        $("#frappe-sign-setup-file").val("");

        this.clear_canvas();
        this.open_modal("#frappe-sign-setup-modal");
    }

    set_setup_mode(mode) {
        this.setup_mode = mode;

        $(".frappe-sign-setup-tabs button")
            .removeClass("btn-primary")
            .addClass("btn-default");

        $(`.frappe-sign-setup-tabs button[data-mode="${mode}"]`)
            .removeClass("btn-default")
            .addClass("btn-primary");

        $(".frappe-sign-setup-pane").prop("hidden", true);
        $(`.frappe-sign-setup-pane[data-pane="${mode}"]`).prop("hidden", false);
    }

    async save_setup_asset() {
        if (!this.setup_kind) {
            return;
        }

        let args = {
            token: this.token,
            kind: this.setup_kind,
            mode: this.setup_mode,
        };

        if (this.setup_mode === "draw") {
            if (!this.canvas || this.canvas.dataset.hasDrawing !== "1") {
                frappe.msgprint(__("Please draw before saving."));
                return;
            }

            args.data_url = this.canvas.toDataURL("image/png");
        }

        if (this.setup_mode === "upload") {
            const file = $("#frappe-sign-setup-file")[0].files[0];

            if (!file) {
                frappe.msgprint(__("Please choose a PNG file."));
                return;
            }

            if (file.type !== "image/png") {
                frappe.msgprint(__("Only PNG files are supported."));
                return;
            }

            args.data_url = await this.read_file_as_data_url(file);
        }

        if (this.setup_mode === "type") {
            const typed_text = $("#frappe-sign-setup-text").val();

            if (!typed_text) {
                frappe.msgprint(__("Please type a value."));
                return;
            }

            args.typed_text = typed_text;
        }

        const response = await frappe.call({
            method: "frappe_sign.api.signing.save_signing_profile_asset",
            args,
            freeze: true,
            freeze_message: __("Saving profile asset..."),
        });

        this.context.profile.signature_type = response.message.signature_type;
        this.context.profile.signature_image = response.message.signature_image;
        this.context.profile.initials_image = response.message.initials_image;
        this.context.profile.signature_text = response.message.signature_text;

        await this.refresh_context();

        frappe.show_alert({
            message: __("Saved."),
            indicator: "green",
        });

        this.close_modal("#frappe-sign-setup-modal");

        if (this.setup_kind === "signature") {
            $(".frappe-sign-signing-field[data-field-type='Signature']").each((i, element) => {
                this.apply_signature_field($(element));
            });
        }

        if (this.setup_kind === "initials") {
            $(".frappe-sign-signing-field[data-field-type='Initials']").each((i, element) => {
                this.apply_initials_field($(element));
            });
        }

        await this.start_required_profile_flow();
    }

    async give_consent() {
        const response = await frappe.call({
            method: "frappe_sign.api.signing.give_consent",
            args: {
                token: this.token,
            },
            freeze: true,
            freeze_message: __("Saving consent..."),
        });

        this.context.profile.consent = response.message.consent;
        this.context.profile.consent_on = response.message.consent_on;

        await this.refresh_context();

        frappe.show_alert({
            message: __("Consent saved."),
            indicator: "green",
        });
    }

    initialise_canvas_once() {
        this.canvas = document.getElementById("frappe-sign-setup-canvas");

        if (!this.canvas) {
            return;
        }

        this.ctx = this.canvas.getContext("2d");

        const ratio = window.devicePixelRatio || 1;
        // Fit the drawing pad to the screen; a fixed 720px pad ran off phones.
        const room = (this.canvas.parentElement && this.canvas.parentElement.clientWidth) || window.innerWidth - 48;
        const css_width = Math.max(240, Math.min(720, room));
        const css_height = Math.round(css_width * 0.38);
        this.canvas.style.touchAction = "none";
        this.canvas.style.maxWidth = "100%";

        this.canvas.style.width = `${css_width}px`;
        this.canvas.style.height = `${css_height}px`;

        this.canvas.width = css_width * ratio;
        this.canvas.height = css_height * ratio;

        this.ctx.scale(ratio, ratio);
        this.ctx.lineWidth = 2.5;
        this.ctx.lineCap = "round";
        this.ctx.lineJoin = "round";
        this.ctx.strokeStyle = "#111827";

        this.canvas.dataset.hasDrawing = "0";

        this.canvas.addEventListener("mousedown", (event) => this.start_drawing(event));
        this.canvas.addEventListener("mousemove", (event) => this.move_drawing(event));
        this.canvas.addEventListener("mouseup", (event) => this.end_drawing(event));
        this.canvas.addEventListener("mouseleave", (event) => this.end_drawing(event));

        this.canvas.addEventListener("touchstart", (event) => this.start_drawing(event), { passive: false });
        this.canvas.addEventListener("touchmove", (event) => this.move_drawing(event), { passive: false });
        this.canvas.addEventListener("touchend", (event) => this.end_drawing(event), { passive: false });
        this.canvas.addEventListener("touchcancel", (event) => this.end_drawing(event), { passive: false });
    }

    get_point(event) {
        const rect = this.canvas.getBoundingClientRect();

        const source = event.touches && event.touches.length
            ? event.touches[0]
            : event;

        return {
            x: source.clientX - rect.left,
            y: source.clientY - rect.top,
        };
    }

    start_drawing(event) {
        this.drawing = true;
        this.last_point = this.get_point(event);
        event.preventDefault();
    }

    move_drawing(event) {
        if (!this.drawing) {
            return;
        }

        const point = this.get_point(event);

        this.ctx.beginPath();
        this.ctx.moveTo(this.last_point.x, this.last_point.y);
        this.ctx.lineTo(point.x, point.y);
        this.ctx.stroke();

        this.last_point = point;
        this.canvas.dataset.hasDrawing = "1";

        event.preventDefault();
    }

    end_drawing(event) {
        this.drawing = false;
        this.last_point = null;

        if (event) {
            event.preventDefault();
        }
    }

    clear_canvas() {
        if (!this.canvas || !this.ctx) {
            return;
        }

        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        this.canvas.dataset.hasDrawing = "0";
    }

    read_file_as_data_url(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();

            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(reader.error);

            reader.readAsDataURL(file);
        });
    }

    collect_field_values() {
        const values = {};

        $(".frappe-sign-signing-field").each(function () {
            const box = $(this);
            const field_name = box.attr("data-field");
            const field_type = box.attr("data-field-type");

            const text_input = box.find("input[type='text']");
            const checkbox = box.find("input[type='checkbox']");

            if (text_input.length) {
                values[field_name] = {
                    field_type,
                    value: text_input.val(),
                    completed: !!text_input.val(),
                };
                return;
            }

            if (checkbox.length) {
                values[field_name] = {
                    field_type,
                    value: checkbox.is(":checked") ? "1" : "0",
                    completed: checkbox.is(":checked"),
                };
                return;
            }

            values[field_name] = {
                field_type,
                value: box.find(".frappe-sign-signing-field-value").text(),
                completed: box.hasClass("completed"),
            };
        });

        return values;
    }

    async complete_signing() {
        if (!this.context.profile.consent) {
            this.open_modal("#frappe-sign-consent-modal");
            return;
        }

        const required = this.get_required_profile_assets();

        if (required.signature && !this.has_signature_available()) {
            this.open_setup_modal("signature");
            return;
        }

        if (required.initials && !this.has_initials_available()) {
            this.open_setup_modal("initials");
            return;
        }

        const required_missing = [];

        $(".frappe-sign-signing-field").each(function () {
            const box = $(this);
            const required = box.attr("data-required") === "1";
            const input = box.find("input");

            if (!required) {
                return;
            }

            if (input.length) {
                if (input.attr("type") === "checkbox") {
                    if (!input.is(":checked")) {
                        required_missing.push(box.attr("data-field"));
                    }
                } else if (!input.val()) {
                    required_missing.push(box.attr("data-field"));
                }

                return;
            }

            if (!box.hasClass("completed")) {
                required_missing.push(box.attr("data-field"));
            }
        });

        if (required_missing.length) {
            frappe.msgprint("Please complete all required signing fields.");
            this.go_to_next_required_field();
            return;
        }

        await frappe.call({
            method: "frappe_sign.api.signing.complete_signing",
            args: {
                token: this.token,
                field_values: JSON.stringify(this.collect_field_values()),
                request_modified: this.context.request_modified,
            },
            freeze: true,
            freeze_message: __("Completing signing..."),
        });

        frappe.msgprint({
            title: __("Signed"),
            message: __("Thank you. The document has been signed."),
            indicator: "green",
        });

        $("#frappe-sign-complete").prop("disabled", true);
        $("#frappe-sign-decline").prop("disabled", true);
    }

    async decline_signing() {
        const reason = window.prompt(__("Please enter a reason for declining this signing request."));

        if (!reason) {
            return;
        }

        await frappe.call({
            method: "frappe_sign.api.signing.decline_signing",
            args: {
                token: this.token,
                reason: reason,
            },
            freeze: true,
            freeze_message: __("Declining signing request..."),
        });

        frappe.msgprint({
            title: __("Declined"),
            message: __("The signing request has been declined."),
            indicator: "red",
        });

        $("#frappe-sign-complete").prop("disabled", true);
        $("#frappe-sign-decline").prop("disabled", true);
    }

    open_modal(selector) {
        $(selector).prop("hidden", false);
    }

    close_modal(selector) {
        $(selector).prop("hidden", true);
    }

    show_error(message) {
        $("#frappe-sign-pdf-container").html(`
            <div class="frappe-sign-message frappe-sign-message-error">
                <h2>Error</h2>
                <p>${frappe.utils.escape_html(message)}</p>
            </div>
        `);
    }
}

frappe.ready(() => {
    const root = document.getElementById("frappe-sign-root");

    if (root) {
        window.frappe_sign_portal = new FrappeSignPortal(root);
    }
});