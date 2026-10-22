// One-page signing for a single signer: read the PDF, type or draw a
// signature, agree, sign. Field positions come from the request, so the
// signer never has to find boxes on the document.
(() => {
    const root = document.getElementById("fs-root");
    if (!root) return;

    const token = root.dataset.token;
    const $ = (id) => document.getElementById(id);
    const api = (method, args) =>
        new Promise((resolve, reject) =>
            frappe.call({
                method: `frappe_sign.api.signing.${method}`,
                args: Object.assign({ token }, args || {}),
                freeze: false,
                callback: (r) => resolve(r.message),
                error: (r) => reject(r),
            })
        );

    let context = null;
    let mode = "type";
    let drawn = false;

    const pdfUrl = `/api/method/frappe_sign.api.signing.get_source_pdf?token=${encodeURIComponent(token)}`;
    $("fs-open").href = pdfUrl;
    $("fs-date").textContent = new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });

    // --- Document ---------------------------------------------------------
    async function renderPdf() {
        const pdf = await window.pdfjsLib.getDocument(pdfUrl).promise;
        const box = $("fs-doc");
        const width = box.clientWidth || window.innerWidth - 24;
        const ratio = Math.min(window.devicePixelRatio || 1, 2.5);
        for (let n = 1; n <= pdf.numPages; n++) {
            const page = await pdf.getPage(n);
            const scale = (width / page.getViewport({ scale: 1 }).width) * ratio;
            const viewport = page.getViewport({ scale });
            const canvas = document.createElement("canvas");
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            canvas.setAttribute("aria-label", `Page ${n} of ${pdf.numPages}`);
            box.appendChild(canvas);
            await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
        }
        $("fs-pages").textContent = `${pdf.numPages} page${pdf.numPages === 1 ? "" : "s"}`;
    }

    // --- Signature: type ----------------------------------------------------
    function updateTyped() {
        const name = $("fs-name").value.trim();
        const typed = $("fs-typed");
        typed.textContent = name || "Type your name above";
        typed.classList.toggle("empty", !name);
    }

    // --- Signature: draw (pointer events cover mouse, touch and pen) ---------
    const canvas = $("fs-canvas");
    const ctx = canvas.getContext("2d");
    let last = null;

    function sizeCanvas() {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return;
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * ratio);
        canvas.height = Math.round(rect.height * ratio);
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.lineWidth = 2.6;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.strokeStyle = "#111";
        drawn = false;
    }

    function point(e) {
        const rect = canvas.getBoundingClientRect();
        return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }

    canvas.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        canvas.setPointerCapture(e.pointerId);
        last = point(e);
        ctx.beginPath();
        ctx.arc(last.x, last.y, 1.2, 0, Math.PI * 2);
        ctx.fillStyle = "#111";
        ctx.fill();
        drawn = true;
    });
    canvas.addEventListener("pointermove", (e) => {
        if (!last) return;
        e.preventDefault();
        const p = point(e);
        ctx.beginPath();
        ctx.moveTo(last.x, last.y);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
        last = p;
    });
    const stop = () => (last = null);
    canvas.addEventListener("pointerup", stop);
    canvas.addEventListener("pointercancel", stop);
    $("fs-clear").addEventListener("click", sizeCanvas);

    document.querySelectorAll(".fs-tab").forEach((tab) =>
        tab.addEventListener("click", () => {
            mode = tab.dataset.mode;
            document.querySelectorAll(".fs-tab").forEach((t) => t.classList.toggle("active", t === tab));
            $("fs-typed").hidden = mode !== "type";
            $("fs-drawbox").hidden = mode !== "draw";
            if (mode === "draw") sizeCanvas();
        })
    );

    // --- Submit -------------------------------------------------------------
    function showError(message) {
        const el = $("fs-error");
        el.textContent = message;
        el.hidden = !message;
        if (message) el.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    function errorText(r) {
        try {
            const msgs = JSON.parse(r?._server_messages || "[]");
            if (msgs.length) return JSON.parse(msgs[0]).message.replace(/<[^>]+>/g, "");
        } catch (e) {}
        return "Something went wrong. Please try again.";
    }

    $("fs-submit").addEventListener("click", async () => {
        showError("");
        const name = $("fs-name").value.trim();
        if (!name) return showError("Please enter your full legal name.");
        if (mode === "draw" && !drawn) return showError("Please draw your signature, or switch to Type.");
        if (!$("fs-agree").checked) return showError("Please tick the box to agree before signing.");

        const button = $("fs-submit");
        button.disabled = true;
        button.textContent = "Signing…";
        try {
            if (context.consent_required && !context.profile.consent) await api("give_consent");
            await api(
                "save_signing_profile_asset",
                mode === "draw"
                    ? { kind: "signature", mode: "draw", data_url: canvas.toDataURL("image/png") }
                    : { kind: "signature", mode: "type", typed_text: name }
            );
            const values = {};
            (context.fields || []).forEach((f) => (values[f.name] = { value: f.field_type === "Checkbox" ? "1" : name }));
            await api("complete_signing", { field_values: JSON.stringify(values) });
            root.hidden = true;
            watcher.disconnect();
            $("fs-jump").hidden = true;
            $("fs-done").hidden = false;
            window.scrollTo({ top: 0, behavior: "smooth" });
        } catch (r) {
            showError(errorText(r));
            button.disabled = false;
            button.textContent = "Sign agreement";
        }
    });

    $("fs-decline").addEventListener("click", async () => {
        const reason = window.prompt("Sorry to hear that. Could you tell us why? (optional)", "");
        if (reason === null) return;
        try {
            await api("decline_signing", { reason: reason || "No reason given" });
            root.innerHTML = '<div class="fs-card fs-msg"><h1>You declined to sign</h1><p>We\'ve let Alternative to Meds Center know.</p></div>';
            watcher.disconnect();
            $("fs-jump").hidden = true;
        } catch (r) {
            showError(errorText(r));
        }
    });

    // "Sign now" shortcut until the signing box is on screen.
    const jump = $("fs-jump");
    jump.addEventListener("click", () => $("fs-sign").scrollIntoView({ behavior: "smooth", block: "start" }));
    const watcher = new IntersectionObserver(([entry]) => (jump.hidden = root.hidden || entry.isIntersecting), { threshold: 0.1 });
    watcher.observe($("fs-sign"));

    // --- Start ----------------------------------------------------------------
    $("fs-name").addEventListener("input", updateTyped);
    api("get_signing_context")
        .then((c) => {
            context = c;
            $("fs-name").value = c.signer?.full_name || c.profile?.full_name || "";
            updateTyped();
        })
        .catch((r) => showError(errorText(r)));
    renderPdf().catch(() => ($("fs-pages").textContent = "Couldn't show the agreement here. Use Open full PDF."));
})();
