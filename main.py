#!/usr/bin/env python3
# -*- coding: utf-8 -*
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
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

# Alerta "CHANCE DE GOL" ao vivo (pressão no jogo)
ALERTA_PRESSAO = os.getenv("ALERTA_PRESSAO", "1") == "1"
PRESS_MIN_MIN = int(os.getenv("PRESS_MIN_MIN", "1"))         # a partir de que minuto (1 = jogo todo)
PRESS_MAX_MIN = int(os.getenv("PRESS_MAX_MIN", "130"))       # até que minuto (130 = inclui acréscimos)
PRESS_MAX_POR_JOGO = int(os.getenv("PRESS_MAX_POR_JOGO", "2"))  # máx. de alertas por jogo
PRESS_INTERVALO = int(os.getenv("PRESS_INTERVALO", "10"))    # min. de espera entre alertas do mesmo jogo
PRESS_JANELA = int(os.getenv("PRESS_JANELA", "10"))          # últimos N minutos analisados
PRESS_FINALIZACOES = int(os.getenv("PRESS_FINALIZACOES", "4"))  # mín. finalizações (2 times) na janela
PRESS_NO_ALVO = int(os.getenv("PRESS_NO_ALVO", "2"))         # mín. finalizações no alvo na janela
PRESS_CANTOS = int(os.getenv("PRESS_CANTOS", "2"))           # mín. cantos na janela
PRESS_SEM_GOL = int(os.getenv("PRESS_SEM_GOL", "3"))         # sem gol nos últimos N minutos
PRESS_DIF_MAX = int(os.getenv("PRESS_DIF_MAX", "1"))         # diferença máxima no placar

# Pré-live (gols no 1º tempo)
PRELIVE_AUTO = os.getenv("PRELIVE_AUTO", "1") == "1"      # envia sozinho 1x por dia
PRELIVE_HORA = int(os.getenv("PRELIVE_HORA", "9"))        # janela HH:00 a HH:29
PRELIVE_TOP = int(os.getenv("PRELIVE_TOP", "3"))          # jogos por liga
HIST_DIAS = int(os.getenv("HIST_DIAS", "45"))             # quantos dias olhar para trás
HIST_JOGOS = int(os.getenv("HIST_JOGOS", "10"))           # últimos N jogos de cada time
MIN_JOGOS_HIST = int(os.getenv("MIN_JOGOS_HIST", "4"))    # mínimo de jogos para entrar no ranking

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
        "ultimo_prelive": "",       # data AAAA-MM-DD do último pré-live automático
        "pressao_ids": {},          # jogos que já receberam alerta de chance de gol
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
    inicio: str = ""       # data/hora de início (ISO, UTC)
    descricao: str = ""    # ex.: "Halftime", "Second Half"

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
            inicio=ev.get("date", "") or "",
            descricao=status.get("type", {}).get("description", "") or "",
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
        "tipo": tipo,                       # "gol" | "vermelho" | "pressao"
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

    if ALERTA_PRESSAO:
        try:
            monitorar_pressao(ao_vivo, estado, memoria)
        except Exception:
            log.exception("Erro no alerta de chance de gol.")

    resolver_sinais(estado, jogos)

    # Limpa memória de jogos que acabaram (só se todas as ligas responderam).
    if todas_ok:
        ids_vivos = {j.id for j in ao_vivo}
        for chave in ("ultimo", "cartoes", "snaps", "gol_min", "total_ant"):
            store = memoria.setdefault(chave, {})
            for mid in list(store):
                if mid not in ids_vivos:
                    store.pop(mid, None)
    return bool(ao_vivo)


# ==========================================================================
# 7b) CHANCE DE GOL AO VIVO (pressão: finalizações + cantos nos últimos minutos)
# ==========================================================================
# Regra (todas precisam bater):
#   - jogo todo: do minuto 1 aos acréscimos (PRESS_MIN_MIN a PRESS_MAX_MIN)
#   - placar empatado ou com diferença <= PRESS_DIF_MAX
#   - nos últimos PRESS_JANELA minutos (ou desde o apito inicial, se o jogo
#     tem menos que isso): finalizações, finalizações no alvo e cantos
#     (somando os dois times) >= os mínimos configurados
#   - sem gol nos últimos PRESS_SEM_GOL minutos
#   - no máximo PRESS_MAX_POR_JOGO alertas por jogo, com PRESS_INTERVALO min entre eles
# O alerta entra na contagem de greens/reds (green = saiu mais 1 gol depois).

NOMES_FINALIZACOES = {"totalShots", "shots"}
NOMES_NO_ALVO = {"shotsOnTarget", "shotsOnGoal"}
NOMES_CANTOS = {"wonCorners", "cornerKicks"}


def minuto_num(relogio: str) -> Optional[int]:
    """'67\'' -> 67 ; '90\'+3\'' -> 93."""
    nums = [int(x) for x in re.findall(r"\d+", relogio or "")]
    return sum(nums[:2]) if nums else None


def _num(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def buscar_stats(j: Jogo) -> Optional[dict]:
    """Estatísticas acumuladas por time: {id_time: {'f': finalizações, 'a': no alvo, 'c': cantos}}."""
    try:
        dados = get_espn(f"{j.liga_slug}/summary?event={j.id}")
        if not dados:
            return None
        out = {}
        for t in dados.get("boxscore", {}).get("teams", []):
            tid = str(t.get("team", {}).get("id", ""))
            reg = {"f": 0, "a": 0, "c": 0}
            for st in t.get("statistics", []):
                nome = st.get("name")
                val = _num(st.get("displayValue", st.get("value")))
                if nome in NOMES_FINALIZACOES:
                    reg["f"] = val
                elif nome in NOMES_NO_ALVO:
                    reg["a"] = val
                elif nome in NOMES_CANTOS:
                    reg["c"] = val
            out[tid] = reg
        if j.id_mandante in out and j.id_visitante in out:
            return out
    except (KeyError, TypeError, AttributeError):
        pass
    return None


def msg_pressao(j: Jogo, minuto: int, d: dict, pressiona: str, janela: int) -> str:
    return "\n".join([
        f"🔥 <b>CHANCE DE GOL</b> — {esc(j.liga)}",
        placar(j),
        f"⏱ {minuto}'",
        f"📊 Últimos {janela} min: <b>{d['f']}</b> finalizações ({d['a']} no alvo) · <b>{d['c']}</b> cantos",
        f"⚡ Pressão: {esc(pressiona)}",
        f"🎯 Referência: mais 1 gol (over {j.total}.5)",
    ])


def monitorar_pressao(ao_vivo: list, estado: dict, memoria: dict) -> None:
    snaps = memoria.setdefault("snaps", {})
    gol_min = memoria.setdefault("gol_min", {})
    total_ant = memoria.setdefault("total_ant", {})
    ids_sinal = estado.setdefault("pressao_ids", {})
    agora_ts = time.time()
    for k, v in list(ids_sinal.items()):          # compatível com o formato antigo
        if not isinstance(v, dict):
            ids_sinal[k] = {"n": 1, "ts": v}
    for k in [k for k, v in ids_sinal.items() if agora_ts - v["ts"] > 86400]:
        ids_sinal.pop(k, None)

    candidatos = []
    for j in ao_vivo:
        m = minuto_num(j.relogio)
        if m is None:
            continue
        ant = total_ant.get(j.id)
        if ant is not None and j.total != ant:
            gol_min[j.id] = m                      # lembra o minuto do último gol
        total_ant[j.id] = j.total
        if j.descricao.strip().lower() == "halftime":
            continue
        info = ids_sinal.get(j.id)
        if info and info["n"] >= PRESS_MAX_POR_JOGO:
            continue
        if m <= PRESS_MAX_MIN:                     # coleta desde o início para ter a "base"
            candidatos.append((j, m))
    if not candidatos:
        return

    with ThreadPoolExecutor(max_workers=6) as ex:       # jogos consultados em paralelo
        resultados = list(ex.map(lambda c: buscar_stats(c[0]), candidatos))

    for (j, m), st in zip(candidatos, resultados):
        if not st:
            continue
        h, a = st[j.id_mandante], st[j.id_visitante]
        snap = (m, h["f"], a["f"], h["a"], a["a"], h["c"], a["c"])
        lista = snaps.setdefault(j.id, [])
        if not lista or lista[-1] != snap:
            lista.append(snap)

        if sum(snap[1:]) == 0 and m >= PRESS_MIN_MIN and not memoria.get("aviso_stats"):
            memoria["aviso_stats"] = True
            log.warning("ESPN sem estatísticas no jogo %s x %s.", j.mandante, j.visitante)
            enviar_telegram("⚠️ A ESPN não trouxe finalizações/cantos para um jogo ao vivo. "
                            "Sem essas estatísticas o alerta de CHANCE DE GOL não dispara nesse jogo.")
            continue

        # ---- condições ----
        if not (PRESS_MIN_MIN <= m <= PRESS_MAX_MIN):
            continue
        if abs(j.gols_mandante - j.gols_visitante) > PRESS_DIF_MAX:
            continue
        ult = gol_min.get(j.id)
        if ult is not None and m - ult < PRESS_SEM_GOL:
            continue
        info = ids_sinal.get(j.id)
        if info and agora_ts - info["ts"] < PRESS_INTERVALO * 60:
            continue                                  # alerta recente neste jogo
        bases = [x for x in lista if x[0] <= m - (PRESS_JANELA - 2)]
        if bases:
            base = bases[-1]
            if m - base[0] > PRESS_JANELA + 5:
                continue                              # base antiga demais (falha de dados)
        elif m <= PRESS_JANELA:
            base = (0, 0, 0, 0, 0, 0, 0)              # início do jogo: tudo zerado
        else:
            continue                                  # ainda sem histórico suficiente
        dh_f, da_f = snap[1] - base[1], snap[2] - base[2]
        d = {
            "f": dh_f + da_f,
            "a": (snap[3] - base[3]) + (snap[4] - base[4]),
            "c": (snap[5] - base[5]) + (snap[6] - base[6]),
        }
        if d["f"] < PRESS_FINALIZACOES or d["a"] < PRESS_NO_ALVO or d["c"] < PRESS_CANTOS:
            continue

        pressiona = j.mandante if dh_f > da_f else j.visitante if da_f > dh_f else "os dois times"
        ids_sinal[j.id] = {"n": (info["n"] if info else 0) + 1, "ts": agora_ts}
        registrar_sinal(estado, j, "pressao", f"{m}'")   # salva o estado também
        enviar_telegram(msg_pressao(j, m, d, pressiona, max(1, m - base[0])))


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
            tipo = {"gol": "Gol", "vermelho": "Vermelho", "pressao": "Chance de gol"}.get(s["tipo"], s["tipo"])
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
# 8b) PRÉ-LIVE: jogos de hoje com mais chance de gol no 1º TEMPO
# ==========================================================================
# Método (simples e transparente): para cada time, olhamos os últimos jogos
# já terminados e contamos os gols marcados no 1º tempo (pelos DOIS lados).
# Para cada jogo de hoje, fazemos a média dos dois times. Quanto maior, mais
# "aberto" costuma ser o 1º tempo. É estatística de histórico, NÃO é garantia.

def gols_1t(competidor: dict) -> Optional[int]:
    """Gols do 1º tempo de um competidor de jogo terminado (None se não houver dado)."""
    ls = competidor.get("linescores") or []
    if len(ls) < 2:               # precisa de pelo menos 2 períodos (1T e 2T)
        return None
    for item in ls:
        if item.get("period") == 1:
            return _int(item.get("value"))
    return _int(ls[0].get("value"))


def gols_1t_por_lances(comp: dict, casa: dict, fora: dict) -> Optional[int]:
    """Plano B: conta os gols até 45' (inclui acréscimos do 1T) na lista de lances."""
    lances = comp.get("details")
    if lances is None:
        return None
    gols = [d for d in lances if d.get("scoringPlay") and not d.get("shootout")]
    if len(gols) != _int(casa.get("score")) + _int(fora.get("score")):
        return None                      # lista incompleta: melhor não usar
    n = 0
    for d in gols:
        nums = re.findall(r"\d+", (d.get("clock") or {}).get("displayValue", "") or "")
        if not nums:
            return None
        if int(nums[0]) <= 45:
            n += 1
    return n


def parse_passado(ev: dict) -> Optional[tuple]:
    """Jogo terminado -> (data, id_casa, id_fora, total_de_gols_no_1T)."""
    try:
        comp = ev["competitions"][0]
        status = ev.get("status") or comp.get("status") or {}
        if status.get("type", {}).get("state") != "post":
            return None
        casa = fora = None
        for c in comp.get("competitors", []):
            if c.get("homeAway") == "home":
                casa = c
            elif c.get("homeAway") == "away":
                fora = c
        if not casa or not fora:
            return None
        hc, hf = gols_1t(casa), gols_1t(fora)
        if hc is not None and hf is not None:
            total = hc + hf
        else:
            total = gols_1t_por_lances(comp, casa, fora)
            if total is None:
                return None
        return (ev.get("date", ""), str(casa["team"]["id"]), str(fora["team"]["id"]), total)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _eventos_intervalo(slug: str, ini, fim, memoria: dict, orcamento: list) -> Optional[list]:
    """Eventos da ESPN entre duas datas. Se a ESPN recusar (HTTP 400), tenta sem 'limit'
    e depois divide o intervalo ao meio. 'orcamento' limita o total de tentativas."""
    a, b = ini.strftime("%Y%m%d"), fim.strftime("%Y%m%d")
    datas = a if ini == fim else f"{a}-{b}"
    variantes = ("&limit=300", "") if memoria.get("usar_limit", True) else ("",)
    for k, sufixo in enumerate(variantes):
        if orcamento[0] <= 0:
            return None
        orcamento[0] -= 1
        dados = get_espn(f"{slug}/scoreboard?dates={datas}{sufixo}")
        if dados is not None:
            if sufixo == "" and k > 0:
                memoria["usar_limit"] = False        # lembra: esta ESPN não aceita 'limit'
            return dados.get("events", [])
    if ini == fim:
        return []                                     # dia problemático: ignora
    meio = ini + (fim - ini) // 2
    esq = _eventos_intervalo(slug, ini, meio, memoria, orcamento)
    dir_ = _eventos_intervalo(slug, meio + timedelta(days=1), fim, memoria, orcamento)
    if esq is None and dir_ is None:
        return None
    return (esq or []) + (dir_ or [])


def carregar_historico(slug: str, memoria: dict) -> Optional[dict]:
    """{id_time: [gols_totais_no_1T dos últimos jogos]}; guardado em cache por dia."""
    hoje_dt = agora().date()
    hoje = hoje_dt.strftime("%Y%m%d")
    cache = memoria.setdefault("hist", {})
    if slug in cache and cache[slug][0] == hoje:
        return cache[slug][1]

    orcamento = [30]                                   # máx. de consultas por liga
    eventos: list = []
    cursor = hoje_dt - timedelta(days=HIST_DIAS)
    falhou = False
    while cursor <= hoje_dt:                           # blocos de 10 dias
        bloco_fim = min(cursor + timedelta(days=9), hoje_dt)
        evs = _eventos_intervalo(slug, cursor, bloco_fim, memoria, orcamento)
        if evs is None:
            falhou = True
        else:
            eventos.extend(evs)
        cursor = bloco_fim + timedelta(days=1)
        time.sleep(PAUSA_ENTRE_REQUISICOES)
    if not eventos and falhou:
        return None                                    # não guarda no cache: tenta de novo depois

    brutos: dict = {}
    vistos = set()
    for ev in eventos:
        if ev.get("id") in vistos:
            continue
        vistos.add(ev.get("id"))
        p = parse_passado(ev)
        if not p:
            continue
        data, id_casa, id_fora, total = p
        brutos.setdefault(id_casa, []).append((data, total))
        brutos.setdefault(id_fora, []).append((data, total))

    times = {}
    for tid, lista in brutos.items():
        lista.sort(reverse=True)                       # mais recentes primeiro
        times[tid] = [t for _, t in lista[:HIST_JOGOS]]
    cache[slug] = (hoje, times)
    return times


def hora_local(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(TZ) if TZ else dt.astimezone()
        return dt.strftime("%H:%M")
    except (ValueError, AttributeError):
        return "--:--"


def montar_prelive(slug: str, memoria: dict, dia: Optional[str] = None) -> Optional[str]:
    """Texto do pré-live de UMA liga (None se não há jogos por começar).
    dia = 'AAAAMMDD' para outra data (ex.: amanhã); None = hoje."""
    diag = memoria.setdefault("diag", {})          # guarda o que aconteceu, para explicar ao usuário
    dados = get_espn(f"{slug}/scoreboard" + (f"?dates={dia}" if dia else ""))
    if dados is None:
        diag[slug] = (slug, 0, 0, False)
        return None
    ligas = dados.get("leagues") or [{}]
    rotulo = ligas[0].get("name") or ligas[0].get("abbreviation") or slug
    todos = []
    for ev in dados.get("events", []):
        j = parse_evento(ev, slug, rotulo)
        if j:
            todos.append(j)
    jogos = [j for j in todos if j.estado == "pre"]
    diag[slug] = (rotulo, len(todos), len(jogos), True)
    if not jogos:
        return None
    sufixo = " (amanhã)" if dia else ""

    hist = carregar_historico(slug, memoria)
    if hist is None:
        hist = {}                  # sem histórico: o bot ainda lista os jogos (sem ranking)

    linhas, sem_dados = [], 0
    for j in jogos:
        a, b = hist.get(j.id_mandante, []), hist.get(j.id_visitante, [])
        if len(a) < MIN_JOGOS_HIST or len(b) < MIN_JOGOS_HIST:
            sem_dados += 1
            continue
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        pa = sum(1 for x in a if x >= 1) / len(a) * 100
        pb = sum(1 for x in b if x >= 1) / len(b) * 100
        linhas.append(((ma + mb) / 2, (pa + pb) / 2, j, ma, mb, pa, pb))
    linhas.sort(key=lambda x: (x[0], x[1]), reverse=True)

    log.info("Pré-live %s: %d jogo(s) por começar, %d com histórico suficiente (%d times com dados).",
             slug, len(jogos), len(linhas), len(hist))
    out = [f"🔮 <b>PRÉ-LIVE — {esc(rotulo)}{sufixo}</b>", "Chance de gol no 1º tempo", ""]
    if not linhas:
        out.append("Sem histórico suficiente de gols no 1T para ranquear. Jogos por começar:")
        for j in jogos[:10]:
            out.append(f"• {esc(j.mandante)} x {esc(j.visitante)} · {hora_local(j.inicio)}")
        return "\n".join(out)
    for i, (media, pct, j, ma, mb, pa, pb) in enumerate(linhas[:PRELIVE_TOP], 1):
        out.append(f"<b>{i}. {esc(j.mandante)} x {esc(j.visitante)}</b> · {hora_local(j.inicio)}")
        out.append(f"   Média de gols no 1T: <b>{media:.2f}</b> ({ma:.1f} | {mb:.1f})")
        out.append(f"   Jogos com gol no 1T: {pa:.0f}% | {pb:.0f}%")
    out.append("")
    out.append(f"ℹ️ Base: últimos {HIST_JOGOS} jogos de cada time. Estatística, não garantia."
               + (f" ({sem_dados} jogo(s) sem dados suficientes)" if sem_dados else ""))
    return "\n".join(out)


def enviar_prelive(memoria: dict, dia: Optional[str] = None) -> None:
    memoria["diag"] = {}
    enviados = 0
    for slug in LIGAS:
        try:
            texto = montar_prelive(slug, memoria, dia)
        except Exception:
            log.exception("Erro no pré-live de %s", slug)
            continue
        if texto:
            enviar_telegram(texto)
            enviados += 1
        time.sleep(PAUSA_ENTRE_REQUISICOES)
    if enviados == 0:
        quando = "amanhã" if dia else "hoje"
        linhas = [f"🔮 Nenhum jogo por começar {quando} nas ligas monitoradas.", "", "Situação por liga:"]
        sem_resposta = 0
        for slug in LIGAS:
            rot, tot, pre, ok = memoria["diag"].get(slug, (slug, 0, 0, False))
            if not ok:
                sem_resposta += 1
                linhas.append(f"• {esc(slug)}: ⚠️ sem resposta da ESPN")
            elif tot == 0:
                linhas.append(f"• {esc(rot)}: sem jogos {quando}")
            else:
                linhas.append(f"• {esc(rot)}: {tot} jogo(s), todos já começaram ou terminaram")
        if sem_resposta == len(LIGAS):
            linhas += ["", "A ESPN não respondeu em nenhuma liga (possível bloqueio ou instabilidade)."]
        elif not dia:
            linhas += ["", "Dica: /prelive amanha mostra os jogos de amanhã."]
        enviar_telegram("\n".join(linhas))


def prelive_em_segundo_plano(memoria: dict, dia: Optional[str] = None) -> None:
    """Monta o pré-live numa thread, para não atrasar os alertas ao vivo."""
    if memoria.get("prelive_rodando"):
        enviar_telegram("⏳ O pré-live anterior ainda está sendo montado. Aguarde um instante.")
        return
    memoria["prelive_rodando"] = True

    def tarefa() -> None:
        try:
            enviar_prelive(memoria, dia)
        except Exception:
            log.exception("Erro ao montar o pré-live.")
        finally:
            memoria["prelive_rodando"] = False

    threading.Thread(target=tarefa, daemon=True).start()


def verificar_prelive_auto(estado: dict, memoria: dict) -> None:
    if not PRELIVE_AUTO:
        return
    agora_ = agora()
    hoje = agora_.strftime("%Y-%m-%d")
    if agora_.hour != PRELIVE_HORA or agora_.minute >= 30:
        return
    if estado.get("ultimo_prelive") == hoje:
        return
    estado["ultimo_prelive"] = hoje      # marca antes: evita repetir se der erro no meio
    salvar_estado(estado)
    prelive_em_segundo_plano(memoria)


def verificar_comandos(memoria: dict) -> None:
    """Lê mensagens novas do Telegram. Atende /prelive só do seu CHAT_ID."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    primeira = memoria.get("offset") is None
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if not primeira:
        params["offset"] = memoria["offset"]
    try:
        r = SESSAO.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return
        resultado = r.json().get("result", [])
    except (requests.RequestException, ValueError) as exc:
        log.debug("getUpdates falhou (%s)", type(exc).__name__)
        return

    for upd in resultado:
        memoria["offset"] = upd["update_id"] + 1
        if primeira:                       # ignora mensagens antigas ao ligar
            continue
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(CHAT_ID):
            continue
        texto = (msg.get("text") or "").strip().lower()
        if texto.startswith("/prelive"):
            partes = texto.split()
            dia = None
            if len(partes) > 1 and partes[1] in ("amanha", "amanhã"):
                dia = (agora() + timedelta(days=1)).strftime("%Y%m%d")
            enviar_telegram("🔎 Montando o pré-live, um instante...")
            prelive_em_segundo_plano(memoria, dia)
    if primeira and memoria.get("offset") is None:
        memoria["offset"] = 0


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
    memoria = {"ultimo": {}, "cartoes": {}, "hist": {}, "offset": None}
    falhas = 0

    enviar_telegram(
        "🤖 <b>Bot iniciado!</b>\n"
        f"Ligas monitoradas: {len(LIGAS)}\n"
        f"Relatório diário: {RELATORIO_HORA:02d}:{RELATORIO_MIN_INICIO:02d}"
        f"–{RELATORIO_HORA:02d}:{RELATORIO_MIN_FIM:02d}\n"
        "Alerta ao vivo: 🔥 CHANCE DE GOL (jogo todo, até os acréscimos)\n"
        "Envie /prelive (ou /prelive amanha) para ver os jogos com mais chance de gol no 1º tempo."
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
            verificar_prelive_auto(estado, memoria)
        except Exception:
            log.exception("Erro no pré-live automático.")

        # Dorme em fatias de 5s e, entre elas, escuta comandos do Telegram (/prelive).
        fim = time.time() + espera
        while time.time() < fim:
            time.sleep(min(5.0, max(0.1, fim - time.time())))
            try:
                verificar_comandos(memoria)
            except Exception:
                log.exception("Erro lendo comandos do Telegram.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot encerrado.")
