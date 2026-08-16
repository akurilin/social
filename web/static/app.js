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
