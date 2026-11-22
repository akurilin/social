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
  let renderCoverage = () => {};
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

    renderCoverage = (result) => {
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
      updated.textContent = result.checked_at ? `Last check: ${formatCheckTime(result.checked_at)}` : "";
    };

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
        renderCoverage(result);
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
      if (event.target.matches("select, input[type='checkbox'], input[type='radio']")
          && !event.target.matches('input[name="source_id"]')) {
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
    const SOURCE_PANEL_EDGE_GAP = 15;
    const form = filter.closest("form");
    const panel = filter.querySelector(".source-filter-panel");
    const boxes = Array.from(filter.querySelectorAll('input[name="source_id"]'));
    const marker = filter.querySelector("[data-source-filter-marker]");
    const summary = filter.querySelector("[data-source-summary]");
    const search = filter.querySelector("[data-source-search]");
    const selectAll = filter.querySelector("[data-source-select-all]");
    const clearAll = filter.querySelector("[data-source-clear-all]");
    const noResults = filter.querySelector("[data-source-no-results]");
    const normalize = (value) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    const visibleBoxes = () => boxes.filter((box) => !box.closest("label").hidden);
    const syncSummary = () => {
      const selected = boxes.filter((box) => box.checked).length;
      if (selected === boxes.length) summary.textContent = "All sources";
      else if (selected === 0) summary.textContent = "No sources";
      else summary.textContent = `${selected} of ${boxes.length} sources`;
    };
    const filterOptions = () => {
      const words = normalize(search.value.trim()).split(/\s+/).filter(Boolean);
      boxes.forEach((box) => {
        const label = box.closest("label");
        const text = normalize(label.textContent);
        label.hidden = !words.every((word) => text.includes(word));
      });
      noResults.hidden = visibleBoxes().length !== 0;
      selectAll.textContent = words.length ? "Select shown" : "Select all";
      clearAll.textContent = words.length ? "Clear shown" : "Clear all";
    };
    const positionPanel = () => {
      if (!filter.open) return;
      panel.style.transform = "";
      const bounds = panel.getBoundingClientRect();
      // Move the wide panel into view when its picker is close to a screen edge.
      const leftShift = SOURCE_PANEL_EDGE_GAP - bounds.left;
      const rightShift = window.innerWidth - SOURCE_PANEL_EDGE_GAP - bounds.right;
      const shift = leftShift > 0 ? leftShift : Math.min(0, rightShift);
      panel.style.transform = `translateX(${shift}px)`;
    };
    const setAll = (checked) => {
      visibleBoxes().forEach((box) => { box.checked = checked; });
      syncSummary();
    };

    boxes.forEach((box) => box.addEventListener("change", syncSummary));
    search.addEventListener("input", filterOptions);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();
    });
    selectAll.addEventListener("click", () => setAll(true));
    clearAll.addEventListener("click", () => setAll(false));
    filter.querySelector("[data-source-apply]").addEventListener(
      "click", () => form?.requestSubmit());
    filter.addEventListener("toggle", () => {
      if (filter.open) {
        positionPanel();
        search.focus();
      }
    });
    window.addEventListener("resize", positionPanel);
    form?.addEventListener("submit", () => {
      if (boxes.length && boxes.every((box) => box.checked)) {
        if (marker) marker.disabled = true;
        boxes.forEach((box) => { box.disabled = true; });
      }
    });
    filterOptions();
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

  const allowCalendarOverlap = "1";
  const setCalendarOverlap = (button, message, confirmed = Boolean(message)) => {
    const tooltip = button.nextElementSibling;
    button.classList.toggle("calendar-add-warning", Boolean(message));
    button.querySelector(".calendar-warning-icon").hidden = !message;
    tooltip.textContent = message;
    if (message) {
      button.setAttribute("aria-describedby", tooltip.id);
    } else {
      button.removeAttribute("aria-describedby");
      tooltip.hidePopover?.();
    }
    if (confirmed) {
      button.name = "allow_overlap";
      button.value = allowCalendarOverlap;
    } else {
      button.removeAttribute("name");
    }
    if (!tooltip.showPopover) button.title = message;
  };
  document.querySelectorAll(".calendar-add-button").forEach((button) => {
    const tooltip = button.nextElementSibling;
    const endInput = button.form.querySelector("input[name='end']");
    endInput?.addEventListener("input", () => {
      // A new end time needs a new confirmation.
      if (button.name === "allow_overlap") setCalendarOverlap(button, "");
    });
    if (!tooltip.showPopover) {
      tooltip.hidden = true;
      if (tooltip.textContent) button.title = tooltip.textContent;
      return;
    }
    let closeTimer;
    const show = () => {
      clearTimeout(closeTimer);
      if (!tooltip.textContent || button.disabled) return;
      tooltip.showPopover();
      const { left, top, bottom } = button.getBoundingClientRect();
      const { width, height } = tooltip.getBoundingClientRect();
      const edge = 8;
      tooltip.style.left = `${Math.max(edge, Math.min(left, document.documentElement.clientWidth - width - edge))}px`;
      tooltip.style.top = `${bottom + height > window.innerHeight && top >= height ? top - height : bottom}px`;
    };
    const hide = () => tooltip.hidePopover();
    const deferHide = () => {
      // Let the pointer move from the button to the tooltip.
      closeTimer = setTimeout(() => {
        if (!button.matches(":hover, :focus-visible") && !tooltip.matches(":hover")) hide();
      }, 120);
    };
    button.addEventListener("pointerenter", show);
    button.addEventListener("pointerleave", deferHide);
    button.addEventListener("focus", show);
    button.addEventListener("blur", deferHide);
    tooltip.addEventListener("pointerleave", deferHide);
    button.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && tooltip.matches(":popover-open")) {
        event.preventDefault();
        hide();
      }
    });
    if (document.activeElement === button) show();
  });

  const calendarEndDialog = document.querySelector("[data-calendar-end-dialog]");
  const calendarListStatus = document.querySelector("[data-calendar-list-status]");
  const calendarEndButton = calendarEndDialog?.querySelector(".calendar-add-button");
  const calendarEndStatus = calendarEndDialog?.querySelector("[data-calendar-end-status]");
  let pendingCalendarForm = null;
  const saveCalendarForm = async (form, button, end = "") => {
    if (button.disabled) return;
    const label = button.querySelector("[data-calendar-add-label]");
    const originalLabel = label.textContent;
    const message = end ? calendarEndStatus : calendarListStatus;
    const endInput = end ? calendarEndDialog.querySelector("input[name='end']") : null;
    if (endInput) endInput.readOnly = true;
    button.nextElementSibling.hidePopover?.();
    button.disabled = true;
    label.textContent = "Saving…";
    const body = new URLSearchParams(new FormData(form));
    if (end) body.set("end", end);
    if (button.name === "allow_overlap") body.set(button.name, button.value);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      const result = await response.json();
      if (!response.ok) {
        if (result.overlap) setCalendarOverlap(button, result.overlap);
        throw new Error(result.detail || "Calendar save failed.");
      }
      // A save can also change the warnings on other rows.
      window.location.reload();
      return;
    } catch (error) {
      if (endInput) endInput.readOnly = false;
      button.disabled = false;
      label.textContent = originalLabel;
      if (message) {
        message.textContent = error.message;
        message.hidden = false;
      }
      button.focus();
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
        const overlap = form.querySelector(".calendar-add-tooltip").textContent;
        setCalendarOverlap(calendarEndButton, overlap, false);
        calendarEndStatus.hidden = true;
        calendarEndDialog.showModal();
      } else {
        saveCalendarForm(form, form.querySelector(".calendar-add-button"));
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
        window.location.reload();
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
      saveCalendarForm(pendingCalendarForm, calendarEndButton, end);
    });
    calendarEndDialog.querySelector("[data-calendar-end-cancel]").addEventListener(
      "click", () => { if (!calendarEndButton.disabled) calendarEndDialog.close(); });
    calendarEndDialog.addEventListener("cancel", (event) => {
      if (calendarEndButton.disabled) event.preventDefault();
    });
    calendarEndDialog.addEventListener("close", () => { pendingCalendarForm = null; });
  }
});
