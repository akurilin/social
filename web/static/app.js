document.addEventListener("DOMContentLoaded", () => {
  const enabled = document.querySelector('input[name="enabled"]');
  const reason = document.querySelector('textarea[name="disabled_reason"]');
  if (enabled && reason) {
    const sync = () => {
      reason.closest("label").classList.toggle("field-muted", enabled.checked);
      reason.required = !enabled.checked;
    };
    enabled.addEventListener("change", sync);
    sync();
  }

  document.querySelectorAll("form[data-auto-submit]").forEach((form) => {
    let searchTimer;
    const submit = () => form.requestSubmit();

    form.addEventListener("change", (event) => {
      if (event.target.matches("select, input[type='checkbox'], input[type='radio']")) {
        submit();
      }
    });

    form.querySelectorAll("[data-submit-on-input]").forEach((control) => {
      control.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(submit, 350);
      });
      control.addEventListener("search", () => {
        window.clearTimeout(searchTimer);
        submit();
      });
    });
  });

  document.querySelectorAll("[data-source-filter]").forEach((filter) => {
    const form = filter.closest("form");
    const boxes = Array.from(filter.querySelectorAll('input[name="source_id"]'));
    const marker = filter.querySelector("[data-source-filter-marker]");
    const summary = filter.querySelector("[data-source-summary]");
    const syncSummary = () => {
      const selected = boxes.filter((box) => box.checked).length;
      if (selected === boxes.length) summary.textContent = "All sources";
      else if (selected === 0) summary.textContent = "No sources";
      else summary.textContent = `${selected} of ${boxes.length} sources`;
    };
    const setAll = (checked) => {
      boxes.forEach((box) => { box.checked = checked; });
      syncSummary();
      form?.requestSubmit();
    };

    boxes.forEach((box) => box.addEventListener("change", syncSummary));
    filter.querySelector("[data-source-select-all]")?.addEventListener(
      "click", () => setAll(true));
    filter.querySelector("[data-source-clear-all]")?.addEventListener(
      "click", () => setAll(false));
    form?.addEventListener("submit", () => {
      if (boxes.length && boxes.every((box) => box.checked)) {
        if (marker) marker.disabled = true;
        boxes.forEach((box) => { box.disabled = true; });
      }
    });
    syncSummary();
  });

  document.addEventListener("click", (event) => {
    document.querySelectorAll("[data-source-filter][open]").forEach((filter) => {
      if (!filter.contains(event.target)) filter.removeAttribute("open");
    });
  });

  document.querySelectorAll("[data-day-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.dayDialog);
      if (dialog) dialog.showModal();
    });
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });
  document.querySelectorAll("dialog.day-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });
});
