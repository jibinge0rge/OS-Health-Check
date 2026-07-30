// Proactively surfaces when Data has moved since this page (or the open
// draft) last knew about it, instead of only discovering it reactively at
// publish time (that's still the safety net -- see the merge/conflict
// logic in editor.js -- this is the "you should always know you're on
// stale data" layer on top of it).

import { state, isDraft } from "./state.js";
import { api } from "./api.js";
import { iconMarkup } from "./icons.js";

const CHECK_INTERVAL_MS = 3 * 60 * 1000;

// Suppresses re-showing the banner for a revision the user already
// dismissed -- but only for that exact revision, so a later publish (a
// new, higher revision) still surfaces.
let dismissedForRevision = null;

export function initStaleness() {
  const banner = document.getElementById("staleness-banner");
  const textEl = document.getElementById("staleness-banner-text");
  const reloadBtn = document.getElementById("staleness-reload-btn");
  const dismissBtn = document.getElementById("staleness-dismiss-btn");
  dismissBtn.innerHTML = iconMarkup("x", { size: 14 });

  let latestKnownRevision = null;

  reloadBtn.addEventListener("click", () => window.location.reload());
  dismissBtn.addEventListener("click", () => {
    dismissedForRevision = latestKnownRevision;
    banner.hidden = true;
  });

  async function check() {
    if (state.screen !== "editor") return;
    if (document.hidden) return;

    let latest;
    try {
      latest = (await api.getLookup("data")).data_revision ?? 0;
    } catch (_err) {
      return; // offline / transient error -- try again next tick
    }
    latestKnownRevision = latest;

    if (isDraft()) {
      if (latest === state.draftBasedOnRevision) {
        banner.hidden = true;
        return;
      }
      if (dismissedForRevision === latest) return;
      textEl.textContent =
        "Data was published again since you started this draft. It's fine to keep editing — " +
        "publishing will merge in what changed and only ask you about rows you both touched.";
      reloadBtn.hidden = true;
      banner.hidden = false;
    } else {
      if (latest === state.dataRevision) {
        banner.hidden = true;
        return;
      }
      if (dismissedForRevision === latest) return;
      textEl.textContent = "Data has been updated since you loaded this page.";
      reloadBtn.hidden = false;
      banner.hidden = false;
    }
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
  window.addEventListener("focus", check);
  setInterval(check, CHECK_INTERVAL_MS);
  check();
}
