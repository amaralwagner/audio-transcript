const MAX_SIZE_MB = 200;
const MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024;
const POLL_INTERVAL_MS = 3000;

const btnUpload = document.getElementById("btn-upload");
const inputFile = document.getElementById("input-file");
const uploadErro = document.getElementById("upload-erro");
const listaCorpo = document.getElementById("lista-corpo");
const modal = document.getElementById("modal");
const modalTitulo = document.getElementById("modal-titulo");
const modalTexto = document.getElementById("modal-texto");
const modalFechar = document.getElementById("modal-fechar");
const btnCopiar = document.getElementById("btn-copiar");
const copiadoMsg = document.getElementById("copiado-msg");

function getToken() {
  let token = localStorage.getItem("access_token");
  if (!token) {
    token = prompt("Informe o token de acesso:");
    if (token) localStorage.setItem("access_token", token);
  }
  return token;
}

async function apiFetch(url, options = {}) {
  const token = getToken();
  const headers = Object.assign({}, options.headers, { "x-access-token": token || "" });
  const resposta = await fetch(url, Object.assign({}, options, { headers }));
  if (resposta.status === 401) {
    localStorage.removeItem("access_token");
    throw new Error("Token de acesso inválido");
  }
  return resposta;
}

function formatarData(isoString) {
  const data = new Date(isoString);
  return data.toLocaleString("pt-BR");
}

function statusHtml(item) {
  if (item.status === "concluido") {
    return '<span class="status concluido"><span class="icone">✔</span> Concluído</span>';
  }
  if (item.status === "erro") {
    return '<span class="status erro"><span class="icone">✖</span> Erro</span>';
  }
  const detalhe = item.progresso
    ? `<span class="status-detalhe">${escapeHtml(item.progresso)}</span>`
    : "";
  return `<span class="status processando"><span class="spinner"></span> Processando</span>${detalhe}`;
}

function renderLista(itens) {
  if (!itens.length) {
    listaCorpo.innerHTML = '<tr id="lista-vazia"><td colspan="4" class="vazio">Nenhuma transcrição ainda.</td></tr>';
    return;
  }

  listaCorpo.innerHTML = itens
    .map((item) => {
      const clicavel = item.status === "concluido" ? "concluido" : "";
      return `
        <tr class="${clicavel}" data-id="${item.id}">
          <td>${escapeHtml(item.nome_arquivo)}</td>
          <td>${formatarData(item.data_envio)}</td>
          <td>${item.tamanho_mb} MB</td>
          <td>${statusHtml(item)}</td>
        </tr>
      `;
    })
    .join("");

  listaCorpo.querySelectorAll("tr.concluido").forEach((tr) => {
    tr.addEventListener("click", () => abrirModal(tr.dataset.id));
  });
}

function escapeHtml(texto) {
  const div = document.createElement("div");
  div.textContent = texto;
  return div.innerHTML;
}

async function carregarLista() {
  try {
    const resposta = await apiFetch("/transcricoes");
    if (!resposta.ok) return;
    const itens = await resposta.json();
    renderLista(itens);
  } catch (err) {
    console.error(err);
  }
}

async function abrirModal(id) {
  const resposta = await apiFetch(`/transcricoes/${id}`);
  if (!resposta.ok) return;
  const item = await resposta.json();
  modalTitulo.textContent = item.nome_arquivo;
  modalTexto.textContent = item.texto_transcrito || "";
  copiadoMsg.hidden = true;
  modal.hidden = false;
}

modalFechar.addEventListener("click", () => {
  modal.hidden = true;
});

modal.addEventListener("click", (evento) => {
  if (evento.target === modal) modal.hidden = true;
});

btnCopiar.addEventListener("click", async () => {
  await navigator.clipboard.writeText(modalTexto.textContent);
  copiadoMsg.hidden = false;
  setTimeout(() => (copiadoMsg.hidden = true), 2000);
});

btnUpload.addEventListener("click", () => inputFile.click());

inputFile.addEventListener("change", async () => {
  const arquivo = inputFile.files[0];
  inputFile.value = "";
  if (!arquivo) return;

  uploadErro.hidden = true;

  if (!arquivo.name.toLowerCase().endsWith(".mp3")) {
    mostrarErro("Apenas arquivos .mp3 são aceitos.");
    return;
  }

  if (arquivo.size > MAX_SIZE_BYTES) {
    mostrarErro(`O arquivo excede o limite de ${MAX_SIZE_MB}MB.`);
    return;
  }

  const formData = new FormData();
  formData.append("file", arquivo);

  btnUpload.disabled = true;
  try {
    const resposta = await apiFetch("/transcrever", {
      method: "POST",
      body: formData,
    });
    if (!resposta.ok) {
      const erro = await resposta.json().catch(() => ({}));
      mostrarErro(erro.detail || "Falha ao enviar o arquivo.");
      return;
    }
    await carregarLista();
  } catch (err) {
    mostrarErro(err.message || "Falha ao enviar o arquivo.");
  } finally {
    btnUpload.disabled = false;
  }
});

function mostrarErro(mensagem) {
  uploadErro.textContent = mensagem;
  uploadErro.hidden = false;
}

carregarLista();
setInterval(carregarLista, POLL_INTERVAL_MS);
