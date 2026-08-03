// Thin fetch wrappers for every /api/* endpoint the app consumes.

async function json(url, opts) {
  const response = await fetch(url, opts);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_err) { /* ignore non-JSON error bodies */ }
    throw new Error(detail || `Request to ${url} failed`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function post(url, body, { signal } = {}) {
  return json(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

function put(url, body) {
  return json(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

// Excel/Parquet export: the response is a file, not JSON -- read it as a
// blob and pull the filename the server chose out of Content-Disposition.
async function postForFile(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const parsed = await response.json();
      detail = parsed.detail || detail;
    } catch (_err) { /* ignore non-JSON error bodies */ }
    throw new Error(detail || `Request to ${url} failed`);
  }
  const match = (response.headers.get("Content-Disposition") || "").match(/filename="?([^"]+)"?/);
  return { blob: await response.blob(), filename: match ? match[1] : "" };
}

export const api = {
  // Lookup (Data / Draft)
  getLookup: (source = "data") => json(`/api/lookup?source=${encodeURIComponent(source)}`),
  saveLookup: (rows, evidence, source = "draft", { baseRows, baseEvidence, resetBase = false } = {}) =>
    post(`/api/lookup?source=${encodeURIComponent(source)}`, {
      rows, evidence, backup_suffix: "",
      base_rows: baseRows, base_evidence: baseEvidence, reset_base: resetBase,
    }),
  validateLookup: (rows, evidence, backupSuffix = "", conflictResolutions = {}) =>
    post(`/api/lookup/validate`, { rows, evidence, backup_suffix: backupSuffix, conflict_resolutions: conflictResolutions }),
  checkPublishConflicts: (rows, evidence) =>
    post(`/api/lookup/validate/check`, { rows, evidence }),
  deleteDraft: () => json(`/api/lookup/draft`, { method: "DELETE" }),
  downloadUrl: (source = "data") => `/api/lookup/download?source=${encodeURIComponent(source)}`,
  exportRowsAsFile: (fmt, rows) => postForFile(`/api/export/${encodeURIComponent(fmt)}`, { rows }),
  getDiff: (source = "draft") => json(`/api/lookup/diff?source=${encodeURIComponent(source)}`),
  getRowEvidence: (osString, source = "data") =>
    json(`/api/lookup/evidence?os_string=${encodeURIComponent(osString)}&source=${encodeURIComponent(source)}`),
  refreshRow: (row, opts) => post(`/api/lookup/row/refresh`, { row }, opts),
  cancelRefreshJob: (jobId) => post(`/api/lookup/refresh/${jobId}/cancel`),

  // Settings
  getSettings: () => json(`/api/settings`),
  putSettings: (payload) => put(`/api/settings`, payload),

  // Deploy: Azure / AWS
  getAzureSettings: () => json(`/api/azure/settings`),
  putAzureSettings: (payload) => put(`/api/azure/settings`, payload),
  getAwsSettings: () => json(`/api/aws/settings`),
  putAwsSettings: (payload) => put(`/api/aws/settings`, payload),

  // Normalization / ambiguity (client-orchestrated Add-OS pipeline)
  normalizeSuggest: (items, allowedPairs, fuzzyThreshold, opts) =>
    post(`/api/normalize-suggest`, {
      items: items.map((os_string) => ({ os_string })),
      allowed_pairs: allowedPairs,
      fuzzy_match_threshold: fuzzyThreshold,
    }, opts),
  ambiguousOsDetect: (osStrings, opts) =>
    post(`/api/ambiguous-os-detect`, { items: osStrings.map((os_string) => ({ os_string })) }, opts),
  eolLookup: (items) => post(`/api/eol-lookup`, { items }),
  vendorLookup: (items) => post(`/api/vendor-lookup`, { items }),

  // Vendor lookups screen
  vendorSources: () => json(`/api/vendor-lookups/sources`),
  vendorStatus: (sourceId) => json(`/api/vendor-lookups/${sourceId}/status`),
  vendorRows: (sourceId) => json(`/api/vendor-lookups/${sourceId}/rows`),
  vendorSettingsGet: () => json(`/api/vendor-lookups/settings`),
  vendorSettingsSave: (sources) => post(`/api/vendor-lookups/settings`, { sources }),
  vendorPreferences: (sourceId, payload) => post(`/api/vendor-lookups/${sourceId}/preferences`, payload),
  vendorSync: (sourceId, payload) => post(`/api/vendor-lookups/${sourceId}/sync`, payload),
  vendorSyncCancel: (jobId) => post(`/api/vendor-lookups/sync/${jobId}/cancel`),

  // OS import
  osImportInspect: (formData) => json(`/api/os-import/inspect`, { method: "POST", body: formData }),
  osImportExtract: (formData) => json(`/api/os-import/extract`, { method: "POST", body: formData }),
};

// SSE helper: POSTs a body and yields parsed `data:` events as they stream in.
export async function* streamEvents(url, body, { signal } = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      const parsed = await response.json();
      detail = parsed.detail || detail;
    } catch (_err) { /* ignore */ }
    throw new Error(detail || `Stream request to ${url} failed`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.split("\n").find((entry) => entry.startsWith("data:"));
      if (!line) continue;
      yield JSON.parse(line.slice(5).trim());
    }
  }
}

export const streams = {
  refreshLookup: (rows, evidence, source, opts, isPartialRefresh = false) =>
    streamEvents("/api/lookup/refresh/stream", { rows, evidence, source, is_partial_refresh: isPartialRefresh }, opts),
  refreshRowsBatch: (rows, opts) =>
    streamEvents("/api/lookup/rows/refresh/stream", { rows }, opts),
  validatePublish: (rows, evidence, backupSuffix, conflictResolutions, opts) =>
    streamEvents(
      "/api/lookup/validate/stream",
      { rows, evidence, backup_suffix: backupSuffix, conflict_resolutions: conflictResolutions || {} },
      opts
    ),
  vendorSync: (sourceId, payload, opts) =>
    streamEvents(`/api/vendor-lookups/${sourceId}/sync/stream`, payload ?? {}, opts),
  azureUpload: (profileId, opts) => streamEvents(`/api/azure/upload?profile_id=${encodeURIComponent(profileId || "")}`, {}, opts),
  awsUpload: (profileId, opts) => streamEvents(`/api/aws/upload?profile_id=${encodeURIComponent(profileId || "")}`, {}, opts),
};
