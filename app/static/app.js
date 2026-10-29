(() => {
  const jsonRequest = async (url, payload) => {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* risposta non JSON */ }
    if (!response.ok) throw new Error(data.detail || data.message || "Richiesta non riuscita.");
    return data;
  };

  const setMessage = (element, message, kind = "") => {
    if (!element) return;
    element.textContent = message;
    element.className = `form-message ${kind}`.trim();
  };

  const requestForm = document.querySelector("#request-code-form");
  const verifyForm = document.querySelector("#verify-code-form");
  const authMessage = document.querySelector("#auth-message");
  let loginEmail = "";

  if (requestForm && verifyForm) {
    requestForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      loginEmail = document.querySelector("#email").value.trim();
      const button = requestForm.querySelector("button[type=submit]");
      button.disabled = true;
      setMessage(authMessage, "Invio della richiesta…");
      try {
        const data = await jsonRequest("/api/auth/request-code", { email: loginEmail });
        requestForm.classList.add("hidden");
        verifyForm.classList.remove("hidden");
        setMessage(authMessage, data.message || "Se l'indirizzo è abilitato, riceverai un codice monouso.", "success");
        document.querySelector("#otp-code").focus();
      } catch (error) {
        setMessage(authMessage, error.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    verifyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = verifyForm.querySelector("button[type=submit]");
      const code = document.querySelector("#otp-code").value.trim();
      button.disabled = true;
      setMessage(authMessage, "Verifica del codice…");
      try {
        const data = await jsonRequest("/api/auth/verify-code", { email: loginEmail, code });
        window.location.assign(data.redirect || "/");
      } catch (error) {
        setMessage(authMessage, error.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    document.querySelector("#change-email")?.addEventListener("click", () => {
      verifyForm.classList.add("hidden");
      requestForm.classList.remove("hidden");
      document.querySelector("#otp-code").value = "";
      setMessage(authMessage, "");
      document.querySelector("#email").focus();
    });
  }

  const logoutButton = document.querySelector("#logout-button");
  if (logoutButton) {
    logoutButton.addEventListener("click", async () => {
      try {
        const data = await jsonRequest("/api/auth/logout", {});
        window.location.assign(data.redirect || "/login");
      } catch (_) {
        window.location.assign("/login");
      }
    });
  }

  const sourceList = document.querySelector("#source-list");
  const queryMessage = document.querySelector("#query-message");

  function addText(parent, tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text || "";
    parent.appendChild(node);
    return node;
  }

  function renderSources(sources) {
    sourceList.replaceChildren();
    for (const source of sources || []) {
      const card = document.createElement("article");
      card.className = "source-card";
      const meta = document.createElement("div");
      meta.className = "source-meta";
      addText(meta, "span", "source-tag", source.source_id || "Fonte");
      addText(meta, "span", "", source.meeting_number ? `Verbale n. ${source.meeting_number}` : "Numero verbale non identificato");
      addText(meta, "span", "", source.meeting_date ? `Data ${source.meeting_date}` : "Data non identificata");
      addText(meta, "span", "", source.page ? `pag. ${source.page}` : "Pagina non identificata");
      card.appendChild(meta);
      addText(card, "h3", "source-title", source.title || "Documento CTS");
      addText(card, "p", "source-excerpt", source.excerpt || "Estratto non disponibile.");
      if (source.document_status && source.document_status !== "processed") {
        addText(card, "p", "source-state", `Stato documento: ${source.document_status}`);
      }
      if (source.ocr_status === "error" || source.ocr_status === "uncertain" || source.ocr_status === "empty") {
        addText(card, "p", "source-state", "Nota: estrazione OCR da verificare.");
      }
      if (source.drive_url) {
        try {
          const url = new URL(source.drive_url);
          if (url.protocol === "https:" && ["drive.google.com", "docs.google.com"].includes(url.hostname)) {
            const link = document.createElement("a");
            link.className = "source-link";
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = "Apri il PDF originale in Drive ↗";
            card.appendChild(link);
          }
        } catch (_) { /* link non valido: resta visibile la citazione interna */ }
      }
      sourceList.appendChild(card);
    }
    document.querySelector("#result-count").textContent = `${(sources || []).length} passaggi`;
    document.querySelector("#results-section").classList.toggle("hidden", !(sources || []).length);
  }

  async function runSearch(mode) {
    const question = document.querySelector("#question").value.trim();
    if (question.length < 2) {
      setMessage(queryMessage, "Scrivi una domanda o un termine da cercare.", "error");
      return;
    }
    const buttons = [document.querySelector("#ask-button"), document.querySelector("#search-button")];
    buttons.forEach((button) => { if (button) button.disabled = true; });
    setMessage(queryMessage, mode === "query" ? "Ricerca delle fonti e preparazione della risposta…" : "Ricerca dei passaggi…");
    document.querySelector("#answer-section")?.classList.add("hidden");
    document.querySelector("#results-section")?.classList.add("hidden");
    const payload = {
      question,
      date_from: document.querySelector("#date-from").value || null,
      date_to: document.querySelector("#date-to").value || null,
      meeting_number: document.querySelector("#meeting-number").value.trim() || null,
    };
    try {
      const data = await jsonRequest(mode === "query" ? "/api/query" : "/api/search", payload);
      if (mode === "query") {
        document.querySelector("#answer-text").textContent = data.answer || "Nessuna risposta disponibile.";
        document.querySelector("#coverage-note").textContent = data.coverage_note || "";
        document.querySelector("#answer-section").classList.remove("hidden");
        renderSources(data.sources || []);
      } else {
        renderSources(data.results || []);
      }
      setMessage(queryMessage, data.count ? "Ricerca completata. Verifica le fonti citate." : "Nessun passaggio indicizzato corrisponde alla richiesta.", data.count ? "success" : "");
    } catch (error) {
      setMessage(queryMessage, error.message, "error");
    } finally {
      buttons.forEach((button) => { if (button) button.disabled = false; });
    }
  }

  document.querySelector("#ask-button")?.addEventListener("click", () => runSearch("query"));
  document.querySelector("#search-button")?.addEventListener("click", () => runSearch("search"));
  document.querySelector("#question")?.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runSearch("query");
  });

  const statusSummary = document.querySelector("#status-summary");
  if (statusSummary) {
    fetch("/api/status", { credentials: "same-origin" })
      .then(async (response) => {
        if (response.status === 401) { window.location.assign("/login"); return null; }
        if (!response.ok) throw new Error("Stato non disponibile");
        return response.json();
      })
      .then((data) => {
        if (!data) return;
        if (!data.total_documents) {
          statusSummary.textContent = "Archivio non ancora importato: le query saranno attive dopo l'indicizzazione integrale dei verbali.";
          return;
        }
        statusSummary.textContent = data.coverage === "complete"
          ? "Elaborazione completa secondo i documenti registrati."
          : "Elaborazione in corso: alcune pagine o documenti potrebbero non essere ancora consultabili.";
        document.querySelector("#status-counts").textContent = `${data.processed_documents} elaborati · ${data.duplicate_documents} copie identiche · ${data.processed_pages}/${data.total_pages} pagine · ${data.indexed_chunks} passaggi · ${data.ocr_pages} OCR${data.ocr_problem_pages ? ` · ${data.ocr_problem_pages} da verificare` : ""}`;
      })
      .catch(() => { statusSummary.textContent = "Non è stato possibile leggere lo stato dell'archivio."; });
  }
})();
