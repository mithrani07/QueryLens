/**
 * script.js
 *
 * QueryLens frontend client logic. Strict vanilla ES6+, zero framework
 * dependencies. Talks to the FastAPI backend (mounted at /api) to upload
 * files, connect to a Postgres database, and generate + preview SQL from
 * a natural language question.
 */

(() => {
  "use strict";

  // ======================================================================
  // State
  // ======================================================================
  const State = {
    activeTab: "file",
    file: null,
    database: null,
    question: "",
    isGenerating: false,
    isConnectingSource: false,
    lastResult: null,
    history: [],
    HISTORY_LIMIT: 20,

    get hasActiveSource() {
      return Boolean(this.file || this.database);
    },

    get activeSourceLabel() {
      if (this.file) return this.file.filename;
      if (this.database) return this.database.connectionInfo?.database || "database";
      return null;
    },

    get activeTables() {
      if (this.file) return this.file.tables;
      if (this.database) return this.database.tables;
      return [];
    },

    setFileSource(record) {
      this.file = record;
      this.database = null;
    },

    setDatabaseSource(record) {
      this.database = record;
      this.file = null;
    },

    clearSource() {
      this.file = null;
      this.database = null;
      this.lastResult = null;
    },

    pushHistory(entry) {
      this.history.unshift(entry);
      if (this.history.length > this.HISTORY_LIMIT) {
        this.history.length = this.HISTORY_LIMIT;
      }
    },
  };

  // ======================================================================
  // API
  // ======================================================================
  const API = (() => {
    const BASE_URL = "/api";
    const DEFAULT_TIMEOUT_MS = 20000;
    const GENERATE_TIMEOUT_MS = 45000;

    class APIError extends Error {
      constructor(message, { status = null, isTimeout = false, isNetwork = false } = {}) {
        super(message);
        this.name = "APIError";
        this.status = status;
        this.isTimeout = isTimeout;
        this.isNetwork = isNetwork;
      }
    }

    async function request(path, { method = "GET", body, isFormData = false, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);

      const headers = {};
      let payload = body;
      if (body !== undefined && !isFormData) {
        headers["Content-Type"] = "application/json";
        payload = JSON.stringify(body);
      }

      let response;
      try {
        response = await fetch(`${BASE_URL}${path}`, {
          method,
          headers,
          body: payload,
          signal: controller.signal,
        });
      } catch (err) {
        clearTimeout(timer);
        if (err.name === "AbortError") {
          throw new APIError("The request took too long and timed out. Please try again.", {
            isTimeout: true,
          });
        }
        throw new APIError("Could not reach the server. Check your connection and try again.", {
          isNetwork: true,
        });
      }
      clearTimeout(timer);

      let data = null;
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        try {
          data = await response.json();
        } catch {
          data = null;
        }
      }

      if (!response.ok) {
        const detail =
          (data && (data.detail || data.message)) ||
          `Request failed with status ${response.status}.`;
        throw new APIError(typeof detail === "string" ? detail : JSON.stringify(detail), {
          status: response.status,
        });
      }

      return data;
    }

    async function uploadFile(file) {
      const formData = new FormData();
      formData.append("file", file);
      return request("/upload", {
        method: "POST",
        body: formData,
        isFormData: true,
        timeoutMs: DEFAULT_TIMEOUT_MS,
      });
    }

    async function deleteUpload(fileId) {
      return request(`/upload/${encodeURIComponent(fileId)}`, {
        method: "DELETE",
        timeoutMs: DEFAULT_TIMEOUT_MS,
      });
    }

    async function connectDatabase(connectionString, schemaFilter) {
      return request("/connect-db", {
        method: "POST",
        body: { connection_string: connectionString, schema_filter: schemaFilter },
        timeoutMs: DEFAULT_TIMEOUT_MS,
      });
    }

    async function generateSQL({ question, fileId, connectionString, schemaFilter, execute = true }) {
      const body = { question, execute };
      if (fileId) body.file_id = fileId;
      if (connectionString) {
        body.connection_string = connectionString;
        body.schema_filter = schemaFilter || "public";
      }
      return request("/generate-sql", {
        method: "POST",
        body,
        timeoutMs: GENERATE_TIMEOUT_MS,
      });
    }

    return { APIError, uploadFile, deleteUpload, connectDatabase, generateSQL };
  })();

  // ======================================================================
  // UI
  // ======================================================================
  const UI = (() => {
    const el = {};

    function cacheElements() {
      el.dropzone = document.getElementById("dropzone");
      el.fileInput = document.getElementById("file-input");

      el.sourceTabs = Array.from(document.querySelectorAll(".source-tab"));
      el.panelFile = document.getElementById("panel-file");
      el.panelDb = document.getElementById("panel-db");

      el.dbForm = document.getElementById("db-form");
      el.dbConnectionString = document.getElementById("db-connection-string");
      el.dbSchema = document.getElementById("db-schema");
      el.connectDbBtn = document.getElementById("connect-db-btn");

      el.schemaPreview = document.getElementById("schema-preview");
      el.schemaFilename = document.getElementById("schema-filename");
      el.schemaClear = document.getElementById("schema-clear");
      el.schemaTables = document.getElementById("schema-tables");

      el.questionInput = document.getElementById("question-input");
      el.suggestions = document.getElementById("suggestions");
      el.generateBtn = document.getElementById("generate-btn");
      el.sourceHint = document.getElementById("source-hint");

      el.outputPanel = document.getElementById("output-panel");
      el.outputLoading = document.getElementById("output-loading");
      el.outputContent = document.getElementById("output-content");
      el.outputError = document.getElementById("output-error");
      el.outputErrorText = document.getElementById("output-error-text");

      el.copyBtn = document.getElementById("copy-btn");
      el.sqlOutput = document.getElementById("sql-output");
      el.explanationOutput = document.getElementById("explanation-output");

      el.resultPreviewBlock = document.getElementById("result-preview-block");
      el.previewMeta = document.getElementById("preview-meta");
      el.resultTableHead = document.getElementById("result-table-head");
      el.resultTableBody = document.getElementById("result-table-body");

      el.toast = document.getElementById("toast");
    }

    let toastTimer = null;
    function showToast(message, durationMs = 2600) {
      if (!el.toast) return;
      el.toast.textContent = message;
      el.toast.classList.add("is-visible");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        el.toast.classList.remove("is-visible");
      }, durationMs);
    }

    function switchSourceTab(tab) {
      el.sourceTabs.forEach((btn) => {
        const isActive = btn.dataset.source === tab;
        btn.classList.toggle("is-active", isActive);
        btn.setAttribute("aria-selected", String(isActive));
      });

      const showFile = tab === "file";
      el.panelFile.classList.toggle("is-active", showFile);
      el.panelFile.hidden = !showFile;
      el.panelDb.classList.toggle("is-active", !showFile);
      el.panelDb.hidden = showFile;
    }

    function setDragOver(isOver) {
      el.dropzone.classList.toggle("is-dragover", isOver);
    }

    function setDropzoneUploading(isUploading) {
      el.dropzone.classList.toggle("is-uploading", isUploading);
      const title = el.dropzone.querySelector(".dropzone-title");
      const subtitle = el.dropzone.querySelector(".dropzone-subtitle");
      if (isUploading) {
        title.dataset.originalText = title.textContent;
        subtitle.dataset.originalText = subtitle.textContent;
        title.textContent = "Uploading…";
        subtitle.textContent = "Reading your file and inferring its schema";
      } else {
        if (title.dataset.originalText) title.textContent = title.dataset.originalText;
        if (subtitle.dataset.originalText) subtitle.textContent = subtitle.dataset.originalText;
      }
    }

    function setConnectDbLoading(isLoading) {
      el.connectDbBtn.disabled = isLoading;
      const label = el.connectDbBtn.querySelector(".btn-label");
      label.textContent = isLoading ? "Connecting…" : "Connect";
    }

    function renderSchemaPreview({ label, tables }) {
      el.schemaFilename.textContent = label;
      el.schemaTables.innerHTML = "";

      tables.forEach((table) => {
        const chip = document.createElement("span");
        chip.className = "schema-table-chip";
        const rowCount = Number.isFinite(table.row_count)
          ? ` · ${table.row_count.toLocaleString()} rows`
          : "";
        chip.textContent = `${table.table_name}${rowCount}`;
        chip.title = table.columns ? table.columns.map((c) => `${c.name} (${c.type})`).join(", ") : "";
        el.schemaTables.appendChild(chip);
      });

      el.schemaPreview.hidden = false;
      setQuestionEnabled(true);
      el.sourceHint.textContent = `Ask a question about ${
        tables.length === 1 ? "this table" : "these tables"
      }.`;
    }

    function clearSchemaPreview() {
      el.schemaPreview.hidden = true;
      el.schemaFilename.textContent = "";
      el.schemaTables.innerHTML = "";
      setQuestionEnabled(false);
      el.sourceHint.textContent = "Upload a file or connect a database to get started.";
      hideOutput();
    }

    function setQuestionEnabled(isEnabled) {
      el.questionInput.disabled = !isEnabled;
      updateGenerateButtonAvailability();
    }

    function updateGenerateButtonAvailability() {
      const hasQuestion = el.questionInput.value.trim().length > 0;
      el.generateBtn.disabled = el.questionInput.disabled || !hasQuestion;
    }

    function setGenerating(isGenerating) {
      el.generateBtn.classList.toggle("is-loading", isGenerating);
      el.questionInput.disabled = isGenerating;
      el.generateBtn.disabled = isGenerating || !el.questionInput.value.trim();
      const label = el.generateBtn.querySelector(".btn-label");
      label.textContent = isGenerating ? "Generating…" : "Generate SQL";
    }

    function showOutputLoading() {
      el.outputPanel.hidden = false;
      el.outputPanel.style.display = "block";

      el.outputLoading.hidden = false;
      el.outputLoading.style.display = "block";

      el.outputContent.hidden = true;
      el.outputContent.style.display = "none";

      el.outputError.hidden = true;
      el.outputError.style.display = "none";

      el.outputPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    function hideOutput() {
      el.outputPanel.hidden = true;
      el.outputPanel.style.display = "none";

      el.outputLoading.hidden = true;
      el.outputLoading.style.display = "none";

      el.outputContent.hidden = true;
      el.outputContent.style.display = "none";

      el.outputError.hidden = true;
      el.outputError.style.display = "none";
    }

    function renderResult({ sql, explanation, resultPreview, warnings }) {
      // Hide loading and error panels explicitly via style.display
      el.outputLoading.hidden = true;
      el.outputLoading.style.display = "none";

      el.outputError.hidden = true;
      el.outputError.style.display = "none";

      // Render SQL
      el.sqlOutput.textContent =
        sql && sql.trim() ? sql : "-- The model could not produce a query for this question.";
      el.sqlOutput.className = "language-sql";

      requestAnimationFrame(() => {
        if (window.Prism && typeof window.Prism.highlightElement === "function") {
          try {
            window.Prism.highlightElement(el.sqlOutput);
          } catch (e) {
            console.warn("Prism highlighting skipped:", e);
          }
        }
      });

      // Render Explanation
      let explanationText = explanation || "";
      if (warnings && warnings.length) {
        explanationText += `\n\n⚠ ${warnings.join(" ")}`;
      }
      el.explanationOutput.textContent = explanationText;

      // Render Result Preview Table safely
      if (resultPreview && Array.isArray(resultPreview.columns) && resultPreview.columns.length > 0) {
        renderResultTable(resultPreview);
        el.resultPreviewBlock.hidden = false;
        el.resultPreviewBlock.style.display = "block";
      } else {
        el.resultPreviewBlock.hidden = true;
        el.resultPreviewBlock.style.display = "none";
        el.resultTableHead.innerHTML = "";
        el.resultTableBody.innerHTML = "";
      }

      el.outputContent.hidden = false;
      el.outputContent.style.display = "block";
      resetCopyButton();
    }

    function renderResultTable(preview) {
      if (!preview) return;

      const columns = Array.isArray(preview.columns) ? preview.columns : [];
      const rows = Array.isArray(preview.rows) ? preview.rows : [];
      const rowCount = typeof preview.row_count === "number" ? preview.row_count : (typeof preview.total_rows === "number" ? preview.total_rows : rows.length);
      const truncated = Boolean(preview.truncated);

      el.resultTableHead.innerHTML = "";
      el.resultTableBody.innerHTML = "";

      const headRow = document.createElement("tr");
      columns.forEach((col) => {
        const th = document.createElement("th");
        th.textContent = col;
        headRow.appendChild(th);
      });
      el.resultTableHead.appendChild(headRow);

      rows.forEach((row) => {
        const tr = document.createElement("tr");
        columns.forEach((col) => {
          const td = document.createElement("td");
          const value = row ? row[col] : "";
          td.textContent = value === null || value === undefined ? "∅" : String(value);
          tr.appendChild(td);
        });
        el.resultTableBody.appendChild(tr);
      });

      const shownCount = rows.length;
      el.previewMeta.textContent = truncated
        ? `Showing ${shownCount.toLocaleString()} of ${rowCount.toLocaleString()}+ rows`
        : `${rowCount.toLocaleString()} row${rowCount === 1 ? "" : "s"}`;
    }

    function renderError(message) {
      el.outputLoading.hidden = true;
      el.outputLoading.style.display = "none";

      el.outputContent.hidden = true;
      el.outputContent.style.display = "none";

      el.outputError.hidden = false;
      el.outputError.style.display = "block";
      el.outputErrorText.textContent = message;

      el.outputPanel.hidden = false;
      el.outputPanel.style.display = "block";
      el.outputPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    let copyResetTimer = null;
    function resetCopyButton() {
      clearTimeout(copyResetTimer);
      el.copyBtn.classList.remove("is-copied");
      el.copyBtn.querySelector(".btn-label").textContent = "Copy";
    }

    async function copySQLToClipboard() {
      const text = el.sqlOutput.textContent || "";
      if (!text.trim()) return;

      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const textarea = document.createElement("textarea");
          textarea.value = text;
          textarea.style.position = "fixed";
          textarea.style.opacity = "0";
          document.body.appendChild(textarea);
          textarea.focus();
          textarea.select();
          document.execCommand("copy");
          document.body.removeChild(textarea);
        }
        el.copyBtn.classList.add("is-copied");
        el.copyBtn.querySelector(".btn-label").textContent = "Copied ✓";
        clearTimeout(copyResetTimer);
        copyResetTimer = setTimeout(resetCopyButton, 2000);
      } catch {
        showToast("Could not copy to clipboard.");
      }
    }

    return {
      cacheElements,
      el,
      showToast,
      switchSourceTab,
      setDragOver,
      setDropzoneUploading,
      setConnectDbLoading,
      renderSchemaPreview,
      clearSchemaPreview,
      setQuestionEnabled,
      updateGenerateButtonAvailability,
      setGenerating,
      showOutputLoading,
      hideOutput,
      renderResult,
      renderError,
      copySQLToClipboard,
      resetCopyButton,
    };
  })();

  // ======================================================================
  // Events
  // ======================================================================
  const Events = (() => {
    const SUPPORTED_EXTENSIONS = [".csv", ".xlsx", ".json"];
    const MAX_UPLOAD_SIZE_MB = 15;

    function getExtension(filename) {
      const idx = filename.lastIndexOf(".");
      return idx === -1 ? "" : filename.slice(idx).toLowerCase();
    }

    function validateFileClientSide(file) {
      const ext = getExtension(file.name);
      if (!SUPPORTED_EXTENSIONS.includes(ext)) {
        return `"${ext || "unknown"}" files aren't supported. Please upload a CSV, Excel (.xlsx), or JSON file.`;
      }
      if (file.size === 0) {
        return "That file is empty.";
      }
      if (file.size > MAX_UPLOAD_SIZE_MB * 1024 * 1024) {
        return `That file is larger than the ${MAX_UPLOAD_SIZE_MB} MB limit.`;
      }
      return null;
    }

    async function handleFileSelected(file) {
      const clientError = validateFileClientSide(file);
      if (clientError) {
        UI.showToast(clientError);
        return;
      }

      const previousFileId = State.file?.fileId;

      UI.setDropzoneUploading(true);
      try {
        const response = await API.uploadFile(file);
        State.setFileSource({
          fileId: response.file_id,
          filename: response.original_filename,
          extension: response.extension,
          sizeBytes: response.size_bytes,
          tables: response.tables,
        });
        UI.renderSchemaPreview({
          label: `📊 ${response.original_filename}`,
          tables: response.tables,
        });
        UI.showToast(
          `Loaded ${response.tables.length} table${
            response.tables.length === 1 ? "" : "s"
          } from ${response.original_filename}.`
        );

        if (previousFileId) {
          API.deleteUpload(previousFileId).catch(() => {});
        }
      } catch (err) {
        const message =
          err instanceof API.APIError ? err.message : "Upload failed unexpectedly. Please try again.";
        UI.showToast(message, 4000);
      } finally {
        UI.setDropzoneUploading(false);
        UI.el.fileInput.value = "";
      }
    }

    async function handleConnectDatabase(evt) {
      evt.preventDefault();
      const connectionString = UI.el.dbConnectionString.value.trim();
      const schemaFilter = UI.el.dbSchema.value.trim() || "public";

      if (!connectionString) {
        UI.showToast("Enter a PostgreSQL connection string first.");
        return;
      }

      UI.setConnectDbLoading(true);
      try {
        const response = await API.connectDatabase(connectionString, schemaFilter);
        State.setDatabaseSource({
          connectionString,
          schemaFilter: response.schema_filter,
          connectionInfo: response.connection,
          tables: response.tables,
        });
        UI.renderSchemaPreview({
          label: `🗄️ ${response.connection.database}@${response.connection.host}`,
          tables: response.tables,
        });
        UI.showToast(
          `Connected — found ${response.tables.length} table${
            response.tables.length === 1 ? "" : "s"
          }.`
        );
      } catch (err) {
        const message =
          err instanceof API.APIError
            ? err.message
            : "Could not connect to the database. Please check the connection string and try again.";
        UI.showToast(message, 4000);
      } finally {
        UI.setConnectDbLoading(false);
      }
    }

    function handleSchemaClear() {
      const fileId = State.file?.fileId;
      State.clearSource();
      UI.clearSchemaPreview();
      UI.el.questionInput.value = "";
      UI.el.dbConnectionString.value = "";
      if (fileId) {
        API.deleteUpload(fileId).catch(() => {});
      }
    }

    function handleSourceTabClick(evt) {
      const tab = evt.currentTarget.dataset.source;
      State.activeTab = tab;
      UI.switchSourceTab(tab);
    }

    function handleSuggestionClick(evt) {
      const question = evt.currentTarget.dataset.question;
      if (!question || UI.el.questionInput.disabled) return;
      UI.el.questionInput.value = question;
      State.question = question;
      UI.updateGenerateButtonAvailability();
      UI.el.questionInput.focus();
    }

    function handleQuestionInput() {
      State.question = UI.el.questionInput.value;
      UI.updateGenerateButtonAvailability();
    }

    function handleQuestionKeydown(evt) {
      if ((evt.metaKey || evt.ctrlKey) && evt.key === "Enter") {
        evt.preventDefault();
        if (!UI.el.generateBtn.disabled) {
          handleGenerateSQL();
        }
      }
    }

    async function handleGenerateSQL() {
      const question = UI.el.questionInput.value.trim();
      if (!question) {
        UI.showToast("Type a question first.");
        return;
      }
      if (!State.hasActiveSource) {
        UI.showToast("Upload a file or connect a database first.");
        return;
      }
      if (State.isGenerating) return;

      State.isGenerating = true;
      State.question = question;
      UI.setGenerating(true);
      UI.showOutputLoading();

      try {
        const params = { question, execute: true };
        if (State.file) {
          params.fileId = State.file.fileId;
        } else if (State.database) {
          params.connectionString = State.database.connectionString;
          params.schemaFilter = State.database.schemaFilter;
        }

        const response = await API.generateSQL(params);

        State.lastResult = {
          sql: response.sql,
          explanation: response.explanation,
          dialect: response.dialect,
          warnings: response.warnings || [],
          resultPreview: response.result_preview || null,
        };
        State.pushHistory({
          question,
          sql: response.sql,
          explanation: response.explanation,
          timestamp: Date.now(),
        });

        UI.renderResult({
          sql: response.sql,
          explanation: response.explanation,
          warnings: response.warnings,
          resultPreview: response.result_preview,
        });
      } catch (err) {
        console.error("Generate SQL Error:", err);
        const message =
          err instanceof API.APIError
            ? err.message
            : "Something went wrong while generating SQL. Please try again.";
        UI.renderError(message);
      } finally {
        State.isGenerating = false;
        UI.setGenerating(false);
        UI.el.questionInput.disabled = false;
        UI.updateGenerateButtonAvailability();
      }
    }

    function handleCopyClick() {
      UI.copySQLToClipboard();
    }

    function preventDefaults(evt) {
      evt.preventDefault();
      evt.stopPropagation();
    }

    function bindDropzone() {
      const zone = UI.el.dropzone;

      ["dragenter", "dragover"].forEach((eventName) => {
        zone.addEventListener(eventName, (evt) => {
          preventDefaults(evt);
          UI.setDragOver(true);
        });
      });

      ["dragleave", "dragend"].forEach((eventName) => {
        zone.addEventListener(eventName, (evt) => {
          preventDefaults(evt);
          if (!zone.contains(evt.relatedTarget)) {
            UI.setDragOver(false);
          }
        });
      });

      zone.addEventListener("drop", (evt) => {
        preventDefaults(evt);
        UI.setDragOver(false);
        const droppedFiles = evt.dataTransfer?.files;
        if (droppedFiles && droppedFiles.length > 0) {
          handleFileSelected(droppedFiles[0]);
        }
      });

      zone.addEventListener("click", () => {
        UI.el.fileInput.click();
      });

      zone.addEventListener("keydown", (evt) => {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          UI.el.fileInput.click();
        }
      });
    }

    function bindAll() {
      bindDropzone();

      UI.el.fileInput.addEventListener("change", (evt) => {
        const files = evt.target.files;
        if (files && files.length > 0) {
          handleFileSelected(files[0]);
        }
      });

      UI.el.sourceTabs.forEach((tab) => {
        tab.addEventListener("click", handleSourceTabClick);
      });

      UI.el.dbForm.addEventListener("submit", handleConnectDatabase);
      UI.el.schemaClear.addEventListener("click", handleSchemaClear);

      UI.el.suggestions.querySelectorAll(".suggestion-chip").forEach((chip) => {
        chip.addEventListener("click", handleSuggestionClick);
      });

      UI.el.questionInput.addEventListener("input", handleQuestionInput);
      UI.el.questionInput.addEventListener("keydown", handleQuestionKeydown);

      UI.el.generateBtn.addEventListener("click", handleGenerateSQL);
      UI.el.copyBtn.addEventListener("click", handleCopyClick);

      window.addEventListener("beforeunload", (evt) => {
        if (State.isGenerating) {
          evt.preventDefault();
          evt.returnValue = "";
        }
      });
    }

    return { bindAll };
  })();

  // ======================================================================
  // Init
  // ======================================================================
  document.addEventListener("DOMContentLoaded", () => {
    UI.cacheElements();
    UI.switchSourceTab(State.activeTab);
    UI.clearSchemaPreview();
    Events.bindAll();
  });
})();