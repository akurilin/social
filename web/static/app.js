document.addEventListener("DOMContentLoaded", () => {
  const FILTER_STORAGE_PREFIX = "social-crawler:filters:v1:";
  const FILTER_PAGES = new Set(["/events", "/calendar"]);
  const PERIOD_FIELD = "period";
  const SOURCE_ID_FIELD = "source_id";
  const SOURCE_MARKER_FIELD = "sources_filtered";
  const filterForm = document.querySelector("form[data-persist-filters]");
  const storageKey = (page) => `${FILTER_STORAGE_PREFIX}${page}`;
  const readFilters = (page) => {
    try {
      return window.localStorage.getItem(storageKey(page));
    } catch {
      return null;
    }
  };
  const saveFilters = () => {
    if (!filterForm) return;
    const params = new URLSearchParams(new FormData(filterForm));
    // Match the server's last period value and keep future sources in All sources.
    const periods = params.getAll(PERIOD_FIELD);
    if (periods.length) params.set(PERIOD_FIELD, periods.at(-1));
    const sources = Array.from(filterForm.querySelectorAll(`input[name="${SOURCE_ID_FIELD}"]`));
    if (sources.length && sources.every((box) => box.checked)) {
      params.delete(SOURCE_MARKER_FIELD);
      params.delete(SOURCE_ID_FIELD);
    }
    try {
      window.localStorage.setItem(
        storageKey(filterForm.dataset.persistFilters), params.toString());
    } catch {
      // Filters still work when the browser blocks storage.
    }
  };

  if (filterForm) {
    // Restore only bare page visits. Explicit URLs remain usable as shared links.
    const saved = readFilters(filterForm.dataset.persistFilters);
    if (!window.location.search && saved) {
      const target = new URL(window.location.href);
      target.search = saved;
      window.location.replace(target.href);
      return;
    }
    saveFilters();
    filterForm.addEventListener("submit", saveFilters);
    filterForm.querySelector("[data-reset-filters]")?.addEventListener("click", () => {
      try {
        window.localStorage.removeItem(storageKey(filterForm.dataset.persistFilters));
      } catch {
        // The reset link also works without storage or JavaScript.
      }
    });
  }

  // View switches use the destination's saved filters, including its month or search.
  document.querySelectorAll(".sidebar a[href], .view-switch a[href]").forEach((link) => {
    const target = new URL(link.href);
    if (target.origin !== window.location.origin || !FILTER_PAGES.has(target.pathname)) return;
    const saved = readFilters(target.pathname.slice(1));
    if (saved !== null) {
      target.search = saved;
      link.href = target.href;
    }
  });

  // Hide only this day's entries; keep the date bar available to reopen them.
  document.querySelectorAll("[data-day-toggle]").forEach((button) => {
    const divider = button.closest(".event-day-divider");
    const dateLabel = divider.querySelector("time").textContent;
    const rows = [];
    let row = divider.nextElementSibling;
    while (row?.classList.contains("event-row")) {
      rows.push(row);
      row = row.nextElementSibling;
    }
    button.addEventListener("click", (event) => {
      // A text selection that ends over the button must not toggle the day.
      const selection = window.getSelection();
      if (event.detail && selection && !selection.isCollapsed
          && selection.containsNode(divider, true)) return;
      const expanded = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(expanded));
      button.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${dateLabel}`);
      rows.forEach((entry) => { entry.hidden = !expanded; });
    });
  });

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

    let refreshing = false;
    refreshButton.addEventListener("click", async () => {
      if (refreshing) {
        refreshButton.disabled = true;
        refreshButton.textContent = "Stopping…";
        try {
          const response = await fetch("/calendar-coverage/stop", {
            method: "POST", headers: { Accept: "application/json" },
          });
          if (!response.ok) throw new Error("Could not stop the refresh. Try again.");
        } catch (error) {
          message.textContent = error.message;
        } finally {
          refreshButton.disabled = false;
          refreshButton.textContent = refreshing ? "Stop refresh" : "Refresh calendar";
        }
        return;
      }
      refreshing = true;
      refreshButton.textContent = "Stop refresh";
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
        if (result.stopped) {
          message.textContent = "Calendar refresh stopped. Displayed colors are from the last check.";
          return;
        }
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
        refreshing = false;
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

  const calendarEndDialog = document.querySelector("[data-calendar-end-dialog]");
  const calendarListStatus = document.querySelector("[data-calendar-list-status]");
  let pendingCalendarForm = null;
  const saveCalendarForm = async (form, end = "") => {
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Saving…";
    const body = new URLSearchParams(new FormData(form));
    if (end) body.set("end", end);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Calendar save failed.");
      const cell = form.closest("td");
      form.hidden = true;
      cell.querySelector("[data-calendar-added]").hidden = false;
      cell.querySelector("[data-calendar-remove]").hidden = false;
      if (calendarListStatus) calendarListStatus.textContent = result.state === "saved"
        ? "Saved in Apple Calendar. Check the linked account for sync."
        : "This event is already in the selected calendar.";
    } catch (error) {
      button.disabled = false;
      button.textContent = "Add";
      if (calendarListStatus) calendarListStatus.textContent = error.message;
    }
  };
  document.querySelectorAll("form[data-calendar-add]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (form.hasAttribute("data-needs-end")) {
        pendingCalendarForm = form;
        calendarEndDialog.querySelector("[data-calendar-end-event]").textContent =
          `${form.dataset.eventTitle} · Starts ${form.dataset.eventStart.replace("T", " ")}`;
        calendarEndDialog.querySelector("input[name='end']").value = "";
        calendarEndDialog.showModal();
      } else {
        saveCalendarForm(form);
      }
    });
  });
  document.querySelectorAll("form[data-calendar-remove]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!window.confirm(`Remove “${form.dataset.eventTitle}” from the selected Apple calendar?`)) return;
      const button = form.querySelector("button[type='submit']");
      button.disabled = true;
      button.textContent = "Removing…";
      try {
        const response = await fetch(form.action, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams(new FormData(form)),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Calendar removal failed.");
        const cell = form.closest("td");
        form.hidden = true;
        cell.querySelector("[data-calendar-added]").hidden = true;
        cell.querySelector("[data-calendar-add]").hidden = false;
        if (calendarListStatus) calendarListStatus.textContent = result.state === "removed"
          ? "Removed from the selected Apple calendar."
          : "This event is no longer in the selected Apple calendar.";
      } catch (error) {
        if (calendarListStatus) calendarListStatus.textContent = error.message;
      } finally {
        button.disabled = false;
        button.textContent = "Remove";
      }
    });
  });
  document.querySelectorAll("form[data-calendar-confirm-remove]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(`Remove “${form.dataset.eventTitle}” from the selected Apple calendar?`)) {
        event.preventDefault();
      }
    });
  });
  if (calendarEndDialog) {
    calendarEndDialog.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (!pendingCalendarForm) return;
      const end = calendarEndDialog.querySelector("input[name='end']").value;
      const form = pendingCalendarForm;
      calendarEndDialog.close();
      saveCalendarForm(form, end);
    });
    calendarEndDialog.querySelector("[data-calendar-end-cancel]").addEventListener(
      "click", () => calendarEndDialog.close());
    calendarEndDialog.addEventListener("close", () => { pendingCalendarForm = null; });
  }
});
