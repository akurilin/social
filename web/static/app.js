document.addEventListener("DOMContentLoaded", () => {
  const coveragePanel = document.querySelector("[data-calendar-coverage]");
  if (coveragePanel) {
    const refreshButton = coveragePanel.querySelector("[data-refresh-calendar]");
    const message = coveragePanel.querySelector("[data-coverage-message]");
    const updated = coveragePanel.querySelector("[data-coverage-updated]");
    const coverageStates = ["planned", "open", "unknown"];
    const coverageSymbols = { planned: "✓", open: "–", unknown: "·" };
    const formatCheckTime = (value) => new Date(value).toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
    const initialTime = updated.querySelector("time");
    if (initialTime) initialTime.textContent = formatCheckTime(initialTime.dateTime);

    refreshButton.addEventListener("click", async () => {
      refreshButton.disabled = true;
      refreshButton.textContent = "Checking…";
      coveragePanel.setAttribute("aria-busy", "true");
      message.textContent = "Reading your calendar and checking social plans…";
      try {
        const response = await fetch("/calendar-coverage/refresh", {
          method: "POST", headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || "Calendar check failed. Try again.");
        }
        const result = await response.json();
        // Rebuild the dates if midnight passed while this page was open.
        const tiles = coveragePanel.querySelectorAll(".coverage-tile");
        if (tiles[0].dataset.coverageDate !== result.days[0].date) {
          window.location.reload();
          return;
        }
        const days = new Map(result.days.map((day) => [day.date, day]));
        // Apply the same saved result to the strip, date rows and month cells.
        document.querySelectorAll("[data-coverage-date]").forEach((element) => {
          const day = days.get(element.dataset.coverageDate);
          coverageStates.forEach((state) => element.classList.remove(`coverage-${state}`));
          if (day) element.classList.add(`coverage-${day.state}`);
          const label = element.querySelector("[data-coverage-label]");
          if (label) label.textContent = day?.label || "";
          const symbol = element.querySelector(".coverage-symbol");
          if (symbol) symbol.textContent = coverageSymbols[day?.state] || "·";
          element.title = day ? `${day.date}: ${day.label}` : "";
        });
        message.textContent = result.message;
        updated.textContent = `Last check: ${formatCheckTime(result.checked_at)}`;
      } catch (error) {
        message.textContent = `${error.message} Displayed colors are from the last check.`;
      } finally {
        refreshButton.disabled = false;
        refreshButton.textContent = "Refresh calendar";
        coveragePanel.removeAttribute("aria-busy");
      }
    });
  }

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
