// scansort UI helpers: folder picker, live filename/target suggestion, keyboard shortcuts.
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  const detail = document.getElementById("detail");
  if (!detail) return;
  const docId = detail.dataset.docId;
  const form = document.getElementById("file-form");
  const filename = document.getElementById("filename");
  const dateInput = document.getElementById("doc-date");
  const target = document.getElementById("target");
  const search = document.getElementById("folder-search");
  const list = document.getElementById("folder-list");
  let filenameEdited = false;

  const selectedLabel = () => form?.querySelector('input[name="label"]:checked')?.value || "";

  async function refreshSuggestion() {
    if (!form) return;
    const params = new URLSearchParams({ label: selectedLabel(), date: dateInput?.value || "" });
    try {
      const res = await fetch(`/doc/${docId}/suggest?${params}`, { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      const data = await res.json();
      target.textContent = data.target || "(choose a folder)";
      if (!filenameEdited && data.filename) filename.value = data.filename;
    } catch (e) { /* offline: keep current values */ }
  }

  if (form) {
    filename.addEventListener("input", () => { filenameEdited = true; });
    form.addEventListener("change", (ev) => {
      if (ev.target.name === "label" || ev.target === dateInput) refreshSuggestion();
    });
    form.addEventListener("submit", (ev) => {
      if (!selectedLabel()) { ev.preventDefault(); alert("Please choose a target folder."); return; }
      document.getElementById("file-btn").disabled = true;
    });
  }

  // folder search
  search?.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    list.querySelectorAll(".folder").forEach((el) => {
      el.classList.toggle("hidden", q !== "" && !el.dataset.label.includes(q));
    });
    list.querySelectorAll(".group").forEach((el) => { el.style.display = q ? "none" : ""; });
  });
  search?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      const first = list.querySelector(".folder:not(.hidden) input");
      if (first) { first.checked = true; refreshSuggestion(); }
    }
  });

  // create folder under the selected one
  document.getElementById("new-folder-btn")?.addEventListener("click", async () => {
    const nameInput = document.getElementById("new-folder-name");
    const name = nameInput.value.trim();
    if (!name) { nameInput.focus(); return; }
    const parent = selectedLabel();
    if (!confirm(`Create folder "${name}" under "${parent || "(root)"}"?`)) return;
    const body = new URLSearchParams({ parent, name, doc_date: dateInput?.value || "" });
    const res = await fetch("/folders/new", {
      method: "POST", body,
      headers: { "X-CSRF-Token": csrf, Accept: "application/json" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { alert(data.error || "Could not create folder"); return; }
    document.getElementById("no-folders")?.remove();
    const label = document.createElement("label");
    label.className = "folder";
    label.dataset.label = data.label.toLowerCase();
    const input = Object.assign(document.createElement("input"), { type: "radio", name: "label", value: data.label });
    label.append(input, " " + data.label + " ");
    list.prepend(label);
    input.checked = true;
    nameInput.value = "";
    refreshSuggestion();
  });

  // keyboard shortcuts
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    const typing = ["input", "textarea", "select"].includes(tag) &&
      !["radio", "checkbox"].includes(document.activeElement.type);
    if (typing) return;
    if (ev.key === "Enter" && ["a", "button", "summary"].includes(tag)) return;
    if (ev.key === "j" && detail.dataset.next) { location.href = detail.dataset.next; }
    else if (ev.key === "k" && detail.dataset.prev) { location.href = detail.dataset.prev; }
    else if (/^[1-9]$/.test(ev.key)) {
      const opt = form?.querySelector(`.proposal-btn[data-key="${ev.key}"] input`);
      if (opt) { opt.checked = true; refreshSuggestion(); ev.preventDefault(); }
    } else if (ev.key === "Enter" && form) { ev.preventDefault(); form.requestSubmit(); }
    else if (ev.key === "/" && search) { ev.preventDefault(); search.focus(); }
  });
})();
