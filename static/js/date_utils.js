// The lookup CSV stores eol_date/eoas_date as unix epoch-second strings
// (confirmed against _data/eol_lookup_evidence.json and eol_service.py).
// These helpers convert that to calendar dates for display + filtering.

export function parseRowDate(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^\d+$/.test(text)) {
    const d = new Date(Number(text) * 1000);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const d = new Date(text);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function toISODate(d) {
  if (!d) return "";
  return d.toISOString().slice(0, 10);
}

export function classifyDateChip(d, kind) {
  if (!d) return "empty";
  const diffDays = (d.getTime() - Date.now()) / 86400000;
  if (diffDays < 0) return "past";
  if (diffDays <= 365) return "soon";
  return kind === "eol" ? "future-eol" : "future-eoas";
}

export function formatRelative(d) {
  if (!d) return "";
  const diffDays = Math.round((d.getTime() - Date.now()) / 86400000);
  const abs = Math.abs(diffDays);
  if (abs > 400) {
    const years = Math.round(abs / 365);
    return diffDays < 0 ? `${years}y ago` : `in ${years}y`;
  }
  if (diffDays === 0) return "today";
  return diffDays < 0 ? `${abs}d ago` : `in ${diffDays}d`;
}
