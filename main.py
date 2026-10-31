#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==========================================================================
 BOT DE FUTEBOL AO VIVO -> TELEGRAM  (Gols e Cartões Vermelhos)
==========================================================================
 - Consulta a API pública da ESPN em loop (while True + time.sleep).
 - Avisa no Telegram a cada GOL e a cada CARTÃO VERMELHO.
 - Cada alerta vira um "sinal" que é avaliado quando o jogo termina:
        GREEN = saiu pelo menos mais 1 gol depois do alerta
        RED   = não saiu nenhum gol depois do alerta
   (regra isolada na função `avaliar_sinal` - fácil de mudar)
 - Todo dia entre 11:00 e 11:30 envia o relatório com greens/reds
   (uma única vez por dia), cobrindo tudo que foi resolvido desde o
   relatório anterior.
 - Estado salvo em arquivo JSON: se o bot reiniciar, não perde o placar.

 CONFIGURAÇÃO (variáveis de ambiente OU edite as constantes abaixo):
   TELEGRAM_TOKEN   token do @BotFather
   CHAT_ID          seu chat id (número)
   TIMEZONE         padrão America/Sao_Paulo
   LIGAS            slugs da ESPN separados por vírgula (opcional)
==========================================================================
"""

import html
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ==========================================================================
# 1) CONFIGURAÇÃO
# ==========================================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "COLE_SEU_TOKEN_AQUI")
CHAT_ID = os.getenv("CHAT_ID", "COLE_SEU_CHAT_ID_AQUI")
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")

LIGAS = [
    s.strip()
    for s in os.getenv(
        "LIGAS",
        "bra.1,eng.1,esp.1,ita.1,ger.1,fra.1,por.1,"
        "uefa.champions,uefa.europa,conmebol.libertadores",
    ).split(",")
    if s.strip()
]

INTERVALO_AO_VIVO = float(os.getenv("INTERVALO_AO_VIVO", "15"))    # s, com jogos rolando
INTERVALO_SEM_JOGOS = float(os.getenv("INTERVALO_SEM_JOGOS", "60"))  # s, sem jogos ao vivo
PAUSA_ENTRE_REQUISICOES = 0.3                                       # s, entre ligas (rate limit)

# Janela do relatório diário (horário local de TIMEZONE)
RELATORIO_HORA = 11
RELATORIO_MIN_INICIO = 0
RELATORIO_MIN_FIM = 30   # exclusivo: vale de 11:00 até 11:29

ARQUIVO_ESTADO = os.getenv("ARQUIVO_ESTADO", "estado_bot.json")
# A ESPN tem mais de um endereço; se um bloquear (HTTP 403), tentamos o outro.
ESPN_HOSTS = [
    "https://site.api.espn.com/apis/site/v2/sports/soccer",
    "https://site.web.api.espn.com/apis/site/v2/sports/soccer",
]
_host_preferido = 0

logging.basicConfig(format="%(asctime)s | %(levelname)-7s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot-futebol")

SESSAO = requests.Session()  # reaproveita a conexão (menos latência)
SESSAO.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
})

try:
    TZ = ZoneInfo(TIMEZONE) if ZoneInfo else None
except Exception:
    TZ = None
    log.warning("Fuso '%s' indisponível; usando o horário do computador. "
                "(No Windows: pip install tzdata)", TIMEZONE)


def agora() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


# ==========================================================================
# 2) TELEGRAM
# ==========================================================================

def enviar_telegram(texto: str) -> bool:
    """Envia uma mensagem HTML. Nunca lança exceção; devolve True/False."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for tentativa in range(3):
        try:
            r = SESSAO.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:  # limite do Telegram: espera o tempo pedido
                espera = r.json().get("parameters", {}).get("retry_after", 3)
                time.sleep(espera + 1)
                continue
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            return False
        except (requests.RequestException, ValueError) as exc:
            # Não imprimimos 'exc' inteiro: ele pode conter a URL (e o token).
            log.warning("Falha de rede no Telegram (%s), tentativa %d/3",
                        type(exc).__name__, tentativa + 1)
            time.sleep(2)
    return False


def esc(texto: Any) -> str:
    return html.escape(str(texto or ""), quote=False)


# ==========================================================================
# 3) ESTADO PERSISTENTE (sinais, totais, último relatório)
# ==========================================================================

def estado_inicial() -> dict:
    return {
        "sinais": [],               # lista de sinais (ver registrar_sinal)
        "totais": {"green": 0, "red": 0},
        "ultimo_relatorio": "",     # data AAAA-MM-DD do último relatório enviado
    }


def carregar_estado() -> dict:
    try:
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            dados = json.load(f)
        base = estado_inicial()
        base.update(dados)
        return base
    except FileNotFoundError:
        return estado_inicial()
    except (OSError, ValueError) as exc:
        log.warning("Estado ilegível (%s); começando do zero.", type(exc).__name__)
        return estado_inicial()


def salvar_estado(estado: dict) -> None:
    try:
        tmp = ARQUIVO_ESTADO + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ARQUIVO_ESTADO)  # gravação atômica
    except OSError as exc:
        log.warning("Não consegui salvar o estado: %s", type(exc).__name__)


# ==========================================================================
# 4) API DA ESPN
# ==========================================================================

def get_json(url: str) -> Optional[dict]:
    """GET com 2 tentativas. Devolve None em caso de falha (nunca levanta erro)."""
    for tentativa in range(2):
        try:
            r = SESSAO.get(url, timeout=(4, 8))
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.0 * (tentativa + 1))
                continue
            log.warning("HTTP %s em %s", r.status_code, url)
            return None
        except (requests.RequestException, ValueError) as exc:
            log.warning("Falha em %s (%s), tentativa %d/2", url, type(exc).__name__, tentativa + 1)
            time.sleep(0.5)
    return None


def get_espn(caminho: str) -> Optional[dict]:
    """Consulta a ESPN tentando os hosts disponíveis (lembra qual funcionou)."""
    global _host_preferido
    n = len(ESPN_HOSTS)
    for i in range(n):
        idx = (_host_preferido + i) % n
        dados = get_json(f"{ESPN_HOSTS[idx]}/{caminho}")
        if dados is not None:
            _host_preferido = idx
            return dados
    return None


@dataclass
class Jogo:
    id: str
    liga_slug: str
    liga: str
    estado: str            # "pre" | "in" | "post"
    relogio: str
    mandante: str
    visitante: str
    id_mandante: str
    id_visitante: str
    gols_mandante: int
    gols_visitante: int
    detalhes: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.gols_mandante + self.gols_visitante


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_evento(ev: dict, slug: str, rotulo: str) -> Optional[Jogo]:
    try:
        comp = ev["competitions"][0]
        casa = fora = None
        for c in comp.get("competitors", []):
            if c.get("homeAway") == "home":
                casa = c
            elif c.get("homeAway") == "away":
                fora = c
        if not casa or not fora:
            return None
        status = ev.get("status") or comp.get("status") or {}

        def nome(c):
            t = c.get("team", {})
            return t.get("shortDisplayName") or t.get("displayName") or "?"

        return Jogo(
            id=str(ev["id"]), liga_slug=slug, liga=rotulo,
            estado=status.get("type", {}).get("state", "pre"),
            relogio=status.get("displayClock", "") or "",
            mandante=nome(casa), visitante=nome(fora),
            id_mandante=str(casa.get("team", {}).get("id", "")),
            id_visitante=str(fora.get("team", {}).get("id", "")),
            gols_mandante=_int(casa.get("score")),
            gols_visitante=_int(fora.get("score")),
            detalhes=comp.get("details") or [],
        )
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def buscar_jogos() -> tuple[dict, int]:
    """Consulta todas as ligas. Devolve ({id: Jogo}, quantas_ligas_responderam)."""
    jogos: dict = {}
    n_ok = 0
    for slug in LIGAS:
        dados = get_espn(f"{slug}/scoreboard")
        if dados is not None:
            n_ok += 1
            ligas = dados.get("leagues") or [{}]
            rotulo = ligas[0].get("abbreviation") or ligas[0].get("name") or slug
            for ev in dados.get("events", []):
                j = parse_evento(ev, slug, rotulo)
                if j:
                    jogos[j.id] = j
        time.sleep(PAUSA_ENTRE_REQUISICOES)  # respeita o rate limit
    return jogos, n_ok


def buscar_total_final(sinal: dict) -> Optional[int]:
    """Plano B: pergunta o resultado final de um jogo que saiu do placar do dia."""
    dados = get_espn(f"{sinal['liga_slug']}/summary?event={sinal['event_id']}")
    if not dados:
        return None
    try:
        comp = dados["header"]["competitions"][0]
        if comp["status"]["type"]["state"] != "post":
            return None
        return sum(_int(c.get("score")) for c in comp["competitors"])
    except (KeyError, IndexError, TypeError):
        return None


# ==========================================================================
# 5) MENSAGENS
# ==========================================================================

def placar(j: Jogo) -> str:
    return f"{esc(j.mandante)} <b>{j.gols_mandante}</b> x <b>{j.gols_visitante}</b> {esc(j.visitante)}"


def nome_jogador(det: Optional[dict]) -> str:
    if not det:
        return ""
    nomes = [a.get("displayName", "") for a in det.get("athletesInvolved", []) if a.get("displayName")]
    return nomes[0] if nomes else ""


def minuto_de(det: Optional[dict], j: Jogo) -> str:
    if det and det.get("clock", {}).get("displayValue"):
        return det["clock"]["displayValue"]
    return j.relogio


def msg_gol(j: Jogo, time_gol: str, det: Optional[dict]) -> str:
    extra = []
    if det and det.get("penaltyKick"):
        extra.append("(pênalti)")
    if det and det.get("ownGoal"):
        extra.append("(gol contra)")
    autor = nome_jogador(det)
    linhas = [
        f"⚽ <b>GOL!</b> — {esc(j.liga)}",
        placar(j),
        f"🎯 <b>{esc(time_gol)}</b>" + (f" · {esc(autor)} {' '.join(extra)}" if autor else ""),
        f"⏱ {esc(minuto_de(det, j))}" if minuto_de(det, j) else "",
    ]
    return "\n".join(l for l in linhas if l)


def msg_gol_anulado(j: Jogo) -> str:
    return f"🚫 <b>GOL ANULADO (VAR?)</b> — {esc(j.liga)}\n{placar(j)}\n⏱ {esc(j.relogio)}"


def msg_vermelho(j: Jogo, det: dict, time_exp: str) -> str:
    autor = nome_jogador(det) or "Jogador"
    linhas = [
        f"🟥 <b>CARTÃO VERMELHO</b> — {esc(j.liga)}",
        placar(j),
        f"👤 {esc(autor)} ({esc(time_exp)})",
        f"⏱ {esc(minuto_de(det, j))}" if minuto_de(det, j) else "",
    ]
    return "\n".join(l for l in linhas if l)


# ==========================================================================
# 6) SINAIS (greens e reds)
# ==========================================================================

def registrar_sinal(estado: dict, j: Jogo, tipo: str, minuto: str) -> None:
    """Guarda o alerta para ser avaliado no fim do jogo."""
    estado["sinais"].append({
        "event_id": j.id,
        "liga_slug": j.liga_slug,
        "tipo": tipo,                       # "gol" | "vermelho"
        "jogo": f"{j.mandante} x {j.visitante}",
        "minuto": minuto,
        "total_no_alerta": j.total,         # gols no momento do alerta
        "placar_no_alerta": f"{j.gols_mandante}-{j.gols_visitante}",
        "ts": time.time(),
        "status": "pendente",               # pendente | green | red | anulado
        "total_final": None,
        "reportado": False,
        "ultima_checagem": 0.0,
    })
    salvar_estado(estado)


def avaliar_sinal(sinal: dict, total_final: int) -> str:
    """
    >>> REGRA DE GREEN/RED (edite aqui se quiser outro critério) <<<
    GREEN: depois do alerta saiu pelo menos mais 1 gol até o fim do jogo.
    RED:   nenhum gol depois do alerta.
    """
    return "green" if total_final > sinal["total_no_alerta"] else "red"


def fechar_sinal(estado: dict, sinal: dict, total_final: int) -> None:
    sinal["total_final"] = total_final
    sinal["status"] = avaliar_sinal(sinal, total_final)
    estado["totais"][sinal["status"]] += 1
    log.info("Sinal %s (%s) => %s", sinal["jogo"], sinal["tipo"], sinal["status"].upper())
    salvar_estado(estado)


def resolver_sinais(estado: dict, jogos: dict) -> None:
    """Fecha os sinais cujos jogos já terminaram."""
    agora_ts = time.time()
    for s in estado["sinais"]:
        if s["status"] != "pendente":
            continue
        idade = agora_ts - s["ts"]
        j = jogos.get(s["event_id"])
        total_final = None

        if j is not None:
            if j.estado == "post":
                total_final = j.total
        elif idade > 2 * 3600 and agora_ts - s.get("ultima_checagem", 0) > 600:
            # Jogo sumiu do placar do dia: pergunta direto (no máx. a cada 10 min).
            s["ultima_checagem"] = agora_ts
            total_final = buscar_total_final(s)

        if total_final is not None:
            fechar_sinal(estado, s, total_final)
        elif idade > 36 * 3600:  # jogo adiado/suspenso: não fica pendente para sempre
            s["status"] = "anulado"
            salvar_estado(estado)


# ==========================================================================
# 7) DETECÇÃO DE EVENTOS
# ==========================================================================

def chave_detalhe(d: dict) -> str:
    return "|".join([
        str((d.get("type") or {}).get("id", "")),
        str((d.get("clock") or {}).get("value", "")),
        str((d.get("team") or {}).get("id", "")),
        nome_jogador(d),
    ])


def detectar_eventos(j: Jogo, estado: dict, memoria: dict) -> None:
    anterior = memoria["ultimo"].get(j.id)
    vistos = memoria["cartoes"].setdefault(j.id, set())
    vermelhos = [d for d in j.detalhes if d.get("redCard")]

    # Primeira vez que vemos o jogo: só memoriza (evita alertas repetidos ao reiniciar).
    if anterior is None:
        vistos.update(chave_detalhe(d) for d in vermelhos)
        memoria["ultimo"][j.id] = j
        return

    # --- GOLS: variação do placar ---
    marcadores = [d for d in j.detalhes if d.get("scoringPlay")]
    ultimo_gol = marcadores[-1] if marcadores and len(marcadores) == j.total else None

    novos = []
    if j.gols_mandante > anterior.gols_mandante:
        novos.append(j.mandante)
    if j.gols_visitante > anterior.gols_visitante:
        novos.append(j.visitante)
    for time_gol in novos:
        registrar_sinal(estado, j, "gol", minuto_de(ultimo_gol, j))
        enviar_telegram(msg_gol(j, time_gol, ultimo_gol))

    if j.gols_mandante < anterior.gols_mandante or j.gols_visitante < anterior.gols_visitante:
        enviar_telegram(msg_gol_anulado(j))

    # --- CARTÕES VERMELHOS: novos itens na lista de lances ---
    for d in vermelhos:
        k = chave_detalhe(d)
        if k in vistos:
            continue
        vistos.add(k)
        tid = str((d.get("team") or {}).get("id", ""))
        time_exp = j.mandante if tid == j.id_mandante else j.visitante if tid == j.id_visitante else "?"
        registrar_sinal(estado, j, "vermelho", minuto_de(d, j))
        enviar_telegram(msg_vermelho(j, d, time_exp))

    memoria["ultimo"][j.id] = j


def ciclo(estado: dict, memoria: dict) -> bool:
    """Uma rodada completa. Devolve True se há jogos ao vivo."""
    jogos, n_ok = buscar_jogos()
    todas_ok = n_ok == len(LIGAS)
    ao_vivo = [j for j in jogos.values() if j.estado == "in"]

    # Se NENHUMA liga responde por muitas rodadas seguidas, avisa uma vez no Telegram.
    if n_ok == 0:
        memoria["rodadas_sem_dados"] = memoria.get("rodadas_sem_dados", 0) + 1
        if memoria["rodadas_sem_dados"] >= 10 and not memoria.get("aviso_api"):
            memoria["aviso_api"] = True
            enviar_telegram("⚠️ <b>Sem dados da ESPN</b> há várias rodadas "
                            "(possível bloqueio HTTP 403 ou instabilidade). "
                            "Sem dados, o bot não consegue enviar alertas.")
    else:
        if memoria.get("aviso_api"):
            enviar_telegram("✅ Dados da ESPN voltaram ao normal.")
        memoria["rodadas_sem_dados"] = 0
        memoria["aviso_api"] = False

    for j in ao_vivo:
        try:
            detectar_eventos(j, estado, memoria)
        except Exception:  # um jogo com dados estranhos não derruba os outros
            log.exception("Erro processando o jogo %s", j.id)

    resolver_sinais(estado, jogos)

    # Limpa memória de jogos que acabaram (só se todas as ligas responderam).
    if todas_ok:
        ids_vivos = {j.id for j in ao_vivo}
        for chave in ("ultimo", "cartoes"):
            for mid in list(memoria[chave]):
                if mid not in ids_vivos:
                    memoria[chave].pop(mid, None)
    return bool(ao_vivo)


# ==========================================================================
# 8) RELATÓRIO DIÁRIO (11:00 - 11:30, uma vez por dia)
# ==========================================================================

def montar_relatorio(estado: dict, data_txt: str) -> tuple[str, list]:
    resolvidos = [s for s in estado["sinais"]
                  if s["status"] in ("green", "red") and not s["reportado"]]
    greens = sum(1 for s in resolvidos if s["status"] == "green")
    reds = len(resolvidos) - greens
    pendentes = sum(1 for s in estado["sinais"] if s["status"] == "pendente")
    taxa = (greens / len(resolvidos) * 100) if resolvidos else 0.0

    linhas = [
        f"📊 <b>RELATÓRIO DIÁRIO — {data_txt}</b>",
        "",
        f"✅ Greens: <b>{greens}</b>",
        f"❌ Reds: <b>{reds}</b>",
        f"🎯 Assertividade: <b>{taxa:.0f}%</b> ({len(resolvidos)} sinais)",
        f"🏆 Placar do dia: <b>{greens} x {reds}</b>",
    ]
    if pendentes:
        linhas.append(f"⏳ Ainda em andamento: {pendentes}")
    tot = estado["totais"]
    linhas += ["", f"📈 Acumulado: {tot['green']} greens / {tot['red']} reds", ""]

    if resolvidos:
        linhas.append("<b>Detalhes:</b>")
        for s in resolvidos[:40]:
            icone = "✅" if s["status"] == "green" else "❌"
            tipo = "Gol" if s["tipo"] == "gol" else "Vermelho"
            linhas.append(
                f"{icone} {esc(s['jogo'])} — {tipo} aos {esc(s['minuto'])} "
                f"(alerta {s['placar_no_alerta']}, final {s['total_final']} gols no total)"
            )
        if len(resolvidos) > 40:
            linhas.append(f"... e mais {len(resolvidos) - 40} sinais.")
    else:
        linhas.append("Nenhum sinal resolvido neste período.")
    return "\n".join(linhas), resolvidos


def verificar_relatorio(estado: dict) -> None:
    agora_ = agora()
    hoje = agora_.strftime("%Y-%m-%d")
    na_janela = (agora_.hour == RELATORIO_HORA
                 and RELATORIO_MIN_INICIO <= agora_.minute < RELATORIO_MIN_FIM)
    if not na_janela or estado["ultimo_relatorio"] == hoje:
        return  # fora da janela, ou já enviado hoje

    texto, resolvidos = montar_relatorio(estado, agora_.strftime("%d/%m/%Y"))
    if enviar_telegram(texto):
        for s in resolvidos:
            s["reportado"] = True
        estado["ultimo_relatorio"] = hoje
        # remove sinais já reportados e fechados, para o arquivo não crescer
        estado["sinais"] = [s for s in estado["sinais"]
                            if s["status"] == "pendente" or not s["reportado"]]
        salvar_estado(estado)
        log.info("Relatório diário enviado.")
    else:
        log.warning("Relatório não enviado; tento de novo na próxima rodada (até 11:30).")


# ==========================================================================
# 9) LOOP PRINCIPAL
# ==========================================================================

def iniciar_servidor_saude() -> None:
    """
    Mini-servidor HTTP em segundo plano. Só liga se existir a variável PORT
    (o Render define sozinho). Serve para o Render aceitar o serviço e para um
    "pinger" (UptimeRobot) manter o bot acordado no plano gratuito.
    """
    porta = os.getenv("PORT")
    if not porta:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Saude(BaseHTTPRequestHandler):
        def _ok(self, corpo: bool) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            if corpo:
                self.wfile.write(b"ok")

        def do_GET(self):
            self._ok(True)

        def do_HEAD(self):
            self._ok(False)

        def log_message(self, *args):  # silencia o log de cada ping
            pass

    def servir() -> None:
        try:
            HTTPServer(("0.0.0.0", int(porta)), Saude).serve_forever()
        except OSError as exc:
            log.warning("Servidor de saúde não iniciou: %s", exc)

    threading.Thread(target=servir, daemon=True).start()
    log.info("Servidor de saúde ativo na porta %s", porta)

def main() -> None:
    if "COLE_SEU" in TELEGRAM_TOKEN or "COLE_SEU" in str(CHAT_ID):
        sys.exit("Configure TELEGRAM_TOKEN e CHAT_ID (variáveis de ambiente ou no topo do arquivo).")

    iniciar_servidor_saude()
    estado = carregar_estado()
    memoria = {"ultimo": {}, "cartoes": {}}
    falhas = 0

    enviar_telegram(
        "🤖 <b>Bot iniciado!</b>\n"
        f"Ligas monitoradas: {len(LIGAS)}\n"
        f"Relatório diário: {RELATORIO_HORA:02d}:{RELATORIO_MIN_INICIO:02d}"
        f"–{RELATORIO_HORA:02d}:{RELATORIO_MIN_FIM:02d}"
    )
    log.info("Bot rodando. Ctrl+C para parar.")

    while True:
        espera = INTERVALO_SEM_JOGOS
        try:
            ao_vivo = ciclo(estado, memoria)
            espera = INTERVALO_AO_VIVO if ao_vivo else INTERVALO_SEM_JOGOS
            falhas = 0
        except KeyboardInterrupt:
            raise
        except Exception:
            falhas += 1
            espera = min(120.0, INTERVALO_AO_VIVO * (2 ** falhas))  # backoff exponencial
            log.exception("Erro na rodada (falha #%d). Nova tentativa em %.0fs.", falhas, espera)

        try:
            verificar_relatorio(estado)
        except Exception:
            log.exception("Erro ao gerar o relatório diário.")

        try:
            time.sleep(espera)
        except KeyboardInterrupt:
            raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot encerrado.")
