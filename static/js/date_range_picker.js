// Lightweight, theme-matched calendar popover -- used both for a date RANGE
// (column filters: pick a start + end day from one calendar) and a single
// DATE (row drawer: pick or clear one date). Replaces native
// <input type="date">: that can't express "pick a range from one calendar"
// at all, and its popup is an OS-native widget that's always light, never
// matching this app's light/dark theme.

const WEEKDAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const MONTH_LABELS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function pad2(n) { return String(n).padStart(2, "0"); }
function toIso(ymd) { return `${ymd.year}-${pad2(ymd.month + 1)}-${pad2(ymd.day)}`; }
function parseIso(iso) {
  if (!iso) return null;
  const [y, m, d] = String(iso).split("-").map(Number);
  if (!y || !m || !d) return null;
  return { year: y, month: m - 1, day: d };
}
function compareYmd(a, b) {
  if (a.year !== b.year) return a.year - b.year;
  if (a.month !== b.month) return a.month - b.month;
  return a.day - b.day;
}
function todayYmd() {
  const d = new Date();
  return { year: d.getFullYear(), month: d.getMonth(), day: d.getDate() };
}

// Only one popover can be open at a time across every picker instance.
let openPicker = null;

function openPopover(triggerEl, build) {
  if (openPicker) {
    const wasThis = openPicker.triggerEl === triggerEl;
    openPicker.close();
    if (wasThis) return null;
  }
  triggerEl.classList.add("is-open");

  const popover = document.createElement("div");
  popover.className = "date-range-popover";
  document.body.appendChild(popover);

  const onDocClick = (event) => {
    if (!popover.contains(event.target) && event.target !== triggerEl) close();
  };
  const onKeydown = (event) => { if (event.key === "Escape") close(); };
  const onReflow = () => position();
  document.addEventListener("click", onDocClick, true);
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("resize", onReflow);
  window.addEventListener("scroll", onReflow, true);

  function close() {
    triggerEl.classList.remove("is-open");
    popover.remove();
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKeydown);
    window.removeEventListener("resize", onReflow);
    window.removeEventListener("scroll", onReflow, true);
    if (openPicker && openPicker.triggerEl === triggerEl) openPicker = null;
  }

  function position() {
    const rect = triggerEl.getBoundingClientRect();
    const popRect = popover.getBoundingClientRect();
    let top = rect.bottom + 4;
    let left = rect.left;
    if (left + popRect.width > window.innerWidth - 8) left = window.innerWidth - popRect.width - 8;
    if (top + popRect.height > window.innerHeight - 8) top = rect.top - popRect.height - 4;
    popover.style.top = `${Math.max(8, top)}px`;
    popover.style.left = `${Math.max(8, left)}px`;
  }

  openPicker = { triggerEl, close };
  build(popover, close, position);
  position();
  return { close, position };
}

/** Renders the header/weekday/grid skeleton once and returns the day
 * buttons indexed by day-of-month, so hover/selection updates afterward
 * only toggle classNames on existing elements instead of tearing down and
 * rebuilding the whole grid on every mouseenter -- rebuilding mid-gesture
 * (mouse moving toward a target while cells keep getting replaced under the
 * cursor) is what made real clicks land on nothing despite the widget
 * looking and working fine under a slower, scripted click. */
function buildSkeleton(popover, { viewYear, viewMonth, onPrev, onNext, onDayClick, onDayHover, footer }) {
  popover.innerHTML = "";

  const header = document.createElement("div");
  header.className = "date-range-header";
  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "date-range-nav";
  prevBtn.textContent = "‹";
  prevBtn.setAttribute("aria-label", "Previous month");
  prevBtn.addEventListener("click", onPrev);
  const label = document.createElement("span");
  label.className = "date-range-month-label";
  label.textContent = `${MONTH_LABELS[viewMonth]} ${viewYear}`;
  const nextBtn = document.createElement("button");
  nextBtn.type = "button";
  nextBtn.className = "date-range-nav";
  nextBtn.textContent = "›";
  nextBtn.setAttribute("aria-label", "Next month");
  nextBtn.addEventListener("click", onNext);
  header.append(prevBtn, label, nextBtn);
  popover.appendChild(header);

  const weekdayRow = document.createElement("div");
  weekdayRow.className = "date-range-weekdays";
  WEEKDAY_LABELS.forEach((w) => {
    const cell = document.createElement("span");
    cell.textContent = w;
    weekdayRow.appendChild(cell);
  });
  popover.appendChild(weekdayRow);

  const grid = document.createElement("div");
  grid.className = "date-range-grid";
  const firstWeekday = new Date(viewYear, viewMonth, 1).getDay();
  const totalDays = new Date(viewYear, viewMonth + 1, 0).getDate();
  const today = todayYmd();
  const dayButtons = {};

  for (let i = 0; i < firstWeekday; i += 1) {
    const blank = document.createElement("span");
    blank.className = "date-range-day is-blank";
    grid.appendChild(blank);
  }
  for (let day = 1; day <= totalDays; day += 1) {
    const ymd = { year: viewYear, month: viewMonth, day };
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "date-range-day";
    cell.textContent = String(day);
    if (compareYmd(ymd, today) === 0) cell.classList.add("is-today");
    cell.addEventListener("mouseenter", () => onDayHover(ymd));
    cell.addEventListener("click", () => onDayClick(ymd));
    grid.appendChild(cell);
    dayButtons[day] = cell;
  }
  popover.appendChild(grid);
  popover.appendChild(footer);
  return dayButtons;
}

/**
 * @param {HTMLElement} triggerEl - button containing a [data-range-label] child.
 * @param {{from: string, to: string}} initial - ISO dates, "" if unset.
 * @param {(range: {from: string, to: string}) => void} onChange
 */
export function initDateRangePicker(triggerEl, initial, onChange) {
  let committedFrom = parseIso(initial.from);
  let committedTo = parseIso(initial.to);

  function updateLabel() {
    const labelEl = triggerEl.querySelector("[data-range-label]");
    if (!committedFrom && !committedTo) {
      labelEl.textContent = "Any date";
      triggerEl.classList.remove("has-value");
      return;
    }
    triggerEl.classList.add("has-value");
    const fromText = committedFrom ? toIso(committedFrom) : "…";
    const toText = committedTo ? toIso(committedTo) : "…";
    labelEl.textContent = fromText === toText ? fromText : `${fromText} – ${toText}`;
  }
  updateLabel();

  triggerEl.addEventListener("click", (event) => {
    event.stopPropagation();
    open();
  });

  function open() {
    let pendingStart = null;
    let hoverDay = null;
    const anchor = committedFrom || committedTo || todayYmd();
    let viewYear = anchor.year;
    let viewMonth = anchor.month;
    let dayButtons = {};

    const footer = document.createElement("div");
    footer.className = "date-range-footer";
    const hint = document.createElement("span");
    hint.className = "date-range-hint";
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn tertiary";
    clearBtn.textContent = "Clear";
    footer.append(hint, clearBtn);

    // `build` runs SYNCHRONOUSLY inside openPopover, before openPopover has
    // returned -- so the `handle` this function assigns to isn't readable
    // yet at that point (using it would throw). The click/Clear handlers
    // below only run later, on an actual click, by which time `handle` is
    // long since assigned, so they're fine; only the *initial* synchronous
    // render must use the `positionFn` argument build() is given directly
    // instead of `handle.position()`. Getting this wrong doesn't throw
    // visibly -- it aborts openPopover() before it sets the popover's
    // position, so the calendar still renders (fully functional) but at the
    // browser's default flow position: appended after everything else in
    // <body>, off the bottom of the page where no click can ever reach it.
    const handle = openPopover(triggerEl, (popover, _close, positionFn) => {
      clearBtn.addEventListener("click", () => {
        committedFrom = null;
        committedTo = null;
        updateLabel();
        onChange({ from: "", to: "" });
        handle.close();
      });
      fullRender(popover, positionFn);
    });
    if (!handle) return; // clicking the trigger while its own popover was open just closes it

    function repaintHighlights() {
      const rangeA = pendingStart || committedFrom;
      const rangeB = pendingStart ? hoverDay : committedTo;
      const lo = rangeA && rangeB && compareYmd(rangeA, rangeB) <= 0 ? rangeA : rangeB;
      const hi = rangeA && rangeB && compareYmd(rangeA, rangeB) <= 0 ? rangeB : rangeA;
      Object.entries(dayButtons).forEach(([dayStr, cell]) => {
        const ymd = { year: viewYear, month: viewMonth, day: Number(dayStr) };
        cell.classList.toggle("is-range-start", Boolean(rangeA) && compareYmd(ymd, rangeA) === 0);
        cell.classList.toggle("is-range-end", Boolean(rangeB) && compareYmd(ymd, rangeB) === 0);
        cell.classList.toggle(
          "is-in-range",
          Boolean(rangeA && rangeB) && compareYmd(ymd, lo) >= 0 && compareYmd(ymd, hi) <= 0
        );
      });
      hint.textContent = pendingStart ? "Pick an end date, or the same day again for one day" : "Pick a start date";
    }

    function fullRender(popover, positionFn) {
      dayButtons = buildSkeleton(popover, {
        viewYear,
        viewMonth,
        onPrev: () => { viewMonth -= 1; if (viewMonth < 0) { viewMonth = 11; viewYear -= 1; } fullRender(popover); },
        onNext: () => { viewMonth += 1; if (viewMonth > 11) { viewMonth = 0; viewYear += 1; } fullRender(popover); },
        onDayHover: (ymd) => {
          if (!pendingStart) return;
          hoverDay = ymd;
          repaintHighlights();
        },
        onDayClick: (ymd) => {
          if (!pendingStart) {
            pendingStart = ymd;
            hoverDay = ymd;
            repaintHighlights();
            return;
          }
          const start = compareYmd(pendingStart, ymd) <= 0 ? pendingStart : ymd;
          const end = compareYmd(pendingStart, ymd) <= 0 ? ymd : pendingStart;
          committedFrom = start;
          committedTo = end;
          updateLabel();
          onChange({ from: toIso(start), to: toIso(end) });
          handle.close();
        },
        footer,
      });
      repaintHighlights();
      (positionFn || handle.position)();
    }
  }

  return {
    setValue(from, to) {
      committedFrom = parseIso(from);
      committedTo = parseIso(to);
      updateLabel();
    },
  };
}

/**
 * Single-date variant (row drawer): click a day to set it immediately, or
 * Clear to blank it -- no two-click range selection.
 * @param {HTMLElement} triggerEl - button containing a [data-range-label] child.
 * @param {string} initialIso - "" if unset.
 * @param {(iso: string) => void} onChange
 */
export function initSingleDatePicker(triggerEl, initialIso, onChange) {
  let committed = parseIso(initialIso);

  function updateLabel() {
    const labelEl = triggerEl.querySelector("[data-range-label]");
    if (!committed) {
      labelEl.textContent = "No date set";
      triggerEl.classList.remove("has-value");
      return;
    }
    triggerEl.classList.add("has-value");
    labelEl.textContent = toIso(committed);
  }
  updateLabel();

  triggerEl.addEventListener("click", (event) => {
    event.stopPropagation();
    if (triggerEl.classList.contains("is-disabled")) return;
    open();
  });

  function open() {
    const anchor = committed || todayYmd();
    let viewYear = anchor.year;
    let viewMonth = anchor.month;
    let dayButtons = {};

    const footer = document.createElement("div");
    footer.className = "date-range-footer";
    const hint = document.createElement("span");
    hint.className = "date-range-hint";
    hint.textContent = "Pick a date";
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn tertiary";
    clearBtn.textContent = "Clear";
    footer.append(hint, clearBtn);

    // See the matching comment in initDateRangePicker's open(): the first
    // render must position via the `positionFn` argument, not `handle`,
    // which isn't assigned yet during this synchronous callback.
    const handle = openPopover(triggerEl, (popover, _close, positionFn) => {
      clearBtn.addEventListener("click", () => {
        committed = null;
        updateLabel();
        onChange("");
        handle.close();
      });
      fullRender(popover, positionFn);
    });
    if (!handle) return;

    function repaintHighlights() {
      Object.entries(dayButtons).forEach(([dayStr, cell]) => {
        const ymd = { year: viewYear, month: viewMonth, day: Number(dayStr) };
        cell.classList.toggle("is-range-start", Boolean(committed) && compareYmd(ymd, committed) === 0);
      });
    }

    function fullRender(popover, positionFn) {
      dayButtons = buildSkeleton(popover, {
        viewYear,
        viewMonth,
        onPrev: () => { viewMonth -= 1; if (viewMonth < 0) { viewMonth = 11; viewYear -= 1; } fullRender(popover); },
        onNext: () => { viewMonth += 1; if (viewMonth > 11) { viewMonth = 0; viewYear += 1; } fullRender(popover); },
        onDayHover: () => {},
        onDayClick: (ymd) => {
          committed = ymd;
          updateLabel();
          onChange(toIso(ymd));
          handle.close();
        },
        footer,
      });
      repaintHighlights();
      (positionFn || handle.position)();
    }
  }

  return {
    setValue(iso) {
      committed = parseIso(iso);
      updateLabel();
    },
    setDisabled(disabled) {
      triggerEl.classList.toggle("is-disabled", disabled);
    },
  };
}
