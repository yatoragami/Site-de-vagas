const status = document.getElementById("status");
const searchButton = document.getElementById("search-button");
const resultsSection = document.getElementById("results-section");
let activeSearch = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function formatPostedDate(value) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return "Data não informada";
  const days = Math.max(0, Math.floor((Date.now() - date.getTime()) / 86_400_000));
  if (days === 0) return "Publicada hoje";
  if (days === 1) return "Publicada ontem";
  return `Publicada há ${days} dias`;
}

function renderJobs(jobs, searchedAt) {
  const list = document.getElementById("results-list");
  document.getElementById("results-title").textContent = "Vagas de TI";
  document.getElementById("results-meta").textContent =
    `${jobs.length} ${jobs.length === 1 ? "vaga" : "vagas"} · atualizada ${new Date(searchedAt).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}`;
  document.getElementById("empty-results").hidden = jobs.length > 0;
  list.innerHTML = jobs.map((job) => `
    <article class="job-card">
      <div class="job-card-top">
        <span class="mode-tag">${escapeHtml(job.work_mode)}</span>
        <span class="date">${escapeHtml(formatPostedDate(job.posted_date))}</span>
      </div>
      <h3>${escapeHtml(job.title)}</h3>
      <p class="company">${escapeHtml(job.company)}</p>
      <p class="location">${escapeHtml(job.location)} · ${escapeHtml(job.source)}</p>
      ${job.snippet ? `<p class="snippet">${escapeHtml(job.snippet)}</p>` : ""}
      <a class="job-link" href="${escapeHtml(job.url)}" target="_blank" rel="noopener noreferrer">Ver vaga e se candidatar <span aria-hidden="true">↗</span></a>
    </article>
  `).join("");
  resultsSection.hidden = false;
}

async function findJobs() {
  if (activeSearch) activeSearch.abort();
  activeSearch = new AbortController();
  searchButton.disabled = true;
  searchButton.textContent = "Buscando…";
  status.classList.remove("error");
  status.textContent = "Buscando vagas de TI e filtrando posições sênior/pleno…";
  document.getElementById("results-list").innerHTML = "";
  document.getElementById("empty-results").hidden = true;
  resultsSection.hidden = false;

  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: activeSearch.signal,
      body: JSON.stringify({
        location: document.getElementById("location").value,
        preference: document.getElementById("preference").value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Não foi possível procurar vagas.");
    renderJobs(payload.jobs, payload.searched_at);
    const failedSources = Object.entries(payload.source_status || {})
      .filter(([, source]) => source.completed < source.requested)
      .map(([name, source]) =>
        `${name} ${source.completed}/${source.requested}${source.error ? ` (${source.error})` : ""}`)
      .join(", ");
    const partialMessage = failedSources
      ? ` Busca parcial; fontes com falha: ${failedSources}.`
      : "";
    const limitMessage = payload.results_limit_reached
      ? ` Exibindo os primeiros ${payload.jobs.length} resultados; refine os filtros para restringir a busca.`
      : "";
    status.textContent = (payload.jobs.length
      ? `${payload.jobs.length} vagas abertas encontradas nos últimos ${payload.window_days} dias, da mais recente para a mais antiga.`
      : "Nenhuma vaga com esses filtros apareceu nos últimos 30 dias.")
      + partialMessage
      + limitMessage;
  } catch (error) {
    if (error.name === "AbortError") return;
    status.textContent = error.message || "Não foi possível procurar vagas. Tente novamente.";
    status.classList.add("error");
    resultsSection.hidden = true;
  } finally {
    if (!activeSearch?.signal.aborted) {
      searchButton.disabled = false;
      searchButton.textContent = "Filtrar vagas";
    }
  }
}

searchButton.addEventListener("click", findJobs);
document.getElementById("location").addEventListener("keydown", (event) => {
  if (event.key === "Enter") findJobs();
});
document.getElementById("preference").addEventListener("change", findJobs);

findJobs();
