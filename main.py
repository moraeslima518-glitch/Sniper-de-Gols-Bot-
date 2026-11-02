#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==========================================================================
 BOT DE FUTEBOL AO VIVO -> TELEGRAM (Gols e Cartões Vermelhos)
==========================================================================
 - Consulta a API pública da ESPN em loop (while True + time.sleep).
 - Avisa no Telegram a cada GOL e a cada CARTÃO VERMELHO.
 - Cada alerta vira um "sinal" que é avaliado quando o jogo termina:
        GREEN = saiu pelo menos mais 1 gol depois do alerta
        RED   = não saiu nenhum gol depois do alerta
 - Todo dia envia o relatório com greens/reds.
 - Estado salvo em arquivo JSON.

 CONFIGURAÇÃO (variáveis de ambiente OU edite as constantes abaixo):
   TELEGRAM_TOKEN   token do @BotFather
   CHAT_ID          seu chat id (número)
   TIMEZONE         padrão America/Sao_Paulo
   LIGAS            slugs da ESPN separados por vírgula
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
except ImportError:
    ZoneInfo = None

# ==========================================================================
# 1) CONFIGURAÇÃO
# ==========================================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "COLE_SEU_TOKEN_AQUI")
CHAT_ID = os.getenv("CHAT_ID", "COLE_SEU_CHAT_ID_AQUI")
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")

# Ligas principais estáveis e testadas
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
PRESS_MIN_MIN = int(os.getenv("PRESS_MIN_MIN", "1"))         # a partir de que minuto
PRESS_MAX_MIN = int(os.getenv("PRESS_MAX_MIN", "130"))       # até que minuto
PRESS_MAX_POR_JOGO = int(os.getenv("PRESS_MAX_POR_JOGO", "2"))  # máx. de alertas por jogo
PRESS_INTERVALO = int(os.getenv("PRESS_INTERVALO", "10"))    # min. de espera entre alertas do mesmo jogo
PRESS_JANELA = int(os.getenv("PRESS_JANELA", "10"))          # últimos N minutos analisados
PRESS_FINALIZACOES = int(os.getenv("PRESS_FINALIZACOES", "4"))  # mín. finalizações na janela
PRESS_NO_ALVO = int(os.getenv("PRESS_NO_ALVO", "2"))         # mín. finalizações no alvo
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

HIST_EXTRA = [x.strip() for x in os.getenv(
    "HIST_EXTRA", "ned.1,tur.1,gre.1,nor.1,aut.1,ukr.1,sco.1,bel.1,den.1,sui.1").split(",") if x.strip()]
FATOR_1T = float(os.getenv("FATOR_1T", "0.45"))

ARQUIVO_ESTADO = os.getenv("ARQUIVO_ESTADO", "estado_bot.json")
ESPN_HOSTS = [
    "https://site.api.espn.com/apis/site/v2/sports/soccer",
    "https://site.web.api.espn.com/apis/site/v2/sports/soccer",
]
_host_preferido = 0

logging.basicConfig(format="%(asctime)s | %(levelname)-7s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot-futebol")

SESSAO = requests.Session()
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
    log.warning("Fuso '%s' indisponível; usando o horário do computador.", TIMEZONE)


def agora() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


# ==========================================================================
# 2) TELEGRAM
# ==========================================================================

def enviar_telegram(texto: str) -> bool:
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
            if r.status_code == 429:
                espera = r.json().get("parameters", {}).get("retry_after", 3)
                time.sleep(espera + 1)
                continue
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            return False
        except (requests.RequestException, ValueError) as exc:
            log.warning("Falha de rede no Telegram (%s), tentativa %d/3", type(exc).__name__, tentativa + 1)
            time.sleep(2)
    return False


def esc(texto: Any) -> str:
    return html.escape(str(texto or ""), quote=False)


# ==========================================================================
# 3) ESTADO PERSISTENTE
# ==========================================================================

def estado_inicial() -> dict:
    return {
        "sinais": [],
        "totais": {"green": 0, "red": 0},
        "ultimo_relatorio": "",
        "ultimo_prelive": "",
        "pressao_ids": {},
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
        os.replace(tmp, ARQUIVO_ESTADO)
    except OSError as exc:
        log.warning("Não consegui salvar o estado: %s", type(exc).__name__)


# ==========================================================================
# 4) API DA ESPN
# ==========================================================================

def get_json(url: str) -> Optional[dict]:
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
    estado: str
    relogio: str
    mandante: str
    visitante: str
    id_mandante: str
    id_visitante: str
    gols_mandante: int
    gols_visitante: int
    detalhes: list = field(default_factory=list)
    inicio: str = ""
    descricao: str = ""

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
        time.sleep(PAUSA_ENTRE_REQUISICOES)
    return jogos, n_ok


def buscar_total_final(sinal: dict) -> Optional[int]:
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
# 6) SINAIS
# ==========================================================================

def registrar_sinal(estado: dict, j: Jogo, tipo: str, minuto: str) -> None:
    estado["sinais"].append({
        "event_id": j.id,
        "liga_slug": j.liga_slug,
        "tipo": tipo,
        "jogo": f"{j.mandante} x {j.visitante}",
        "minuto": minuto,
        "total_no_alerta": j.total,
        "placar_no_alerta": f"{j.gols_mandante}-{j.gols_visitante}",
        "ts": time.time(),
        "status": "pendente",
        "total_final": None,
        "reportado": False,
        "ultima_checagem": 0.0,
    })
    salvar_estado(estado)


def avaliar_sinal(sinal: dict, total_final: int) -> str:
    return "green" if total_final > sinal["total_no_alerta"] else "red"


def fechar_sinal(estado: dict, sinal: dict, total_final: int) -> None:
    sinal["total_final"] = total_final
    sinal["status"] = avaliar_sinal(sinal, total_final)
    estado["totais"][sinal["status"]] += 1
    log.info("Sinal %s (%s) => %s", sinal["jogo"], sinal["tipo"], sinal["status"].upper())
    salvar_estado(estado)


def resolver_sinais(estado: dict, jogos: dict) -> None:
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
            s["ultima_checagem"] = agora_ts
            total_final = buscar_total_final(s)

        if total_final is not None:
            fechar_sinal(estado, s, total_final)
        elif idade > 36 * 3600:
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

    if anterior is None:
        vistos.update(chave_detalhe(d) for d in vermelhos)
        memoria["ultimo"][j.id] = j
        return

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
    jogos, n_ok = buscar_jogos()
    todas_ok = n_ok == len(LIGAS)
    ao_vivo = [j for j in jogos.values() if j.estado == "in"]

    if n_ok == 0:
        memoria["rodadas_sem_dados"] = memoria.get("rodadas_sem_dados", 0) + 1
        if memoria["rodadas_sem_dados"] >= 10 and not memoria.get("aviso_api"):
            memoria["aviso_api"] = True
            enviar_telegram("⚠️ <b>Sem dados da ESPN</b> há várias rodadas.")
    else:
        if memoria.get("aviso_api"):
            enviar_telegram("✅ Dados da ESPN voltaram ao normal.")
        memoria["rodadas_sem_dados"] = 0
        memoria["aviso_api"] = False

    for j in ao_vivo:
        try:
            detectar_eventos(j, estado, memoria)
        except Exception:
            log.exception("Erro processando o jogo %s", j.id)

    if ALERTA_PRESSAO:
        try:
            monitorar_pressao(ao_vivo, estado, memoria)
        except Exception:
            log.exception("Erro no alerta de chance de gol.")

    resolver_sinais(estado, jogos)

    if todas_ok:
        ids_vivos = {j.id for j in ao_vivo}
        for chave in ("ultimo", "cartoes", "snaps", "gol_min", "total_ant"):
            store = memoria.setdefault(chave, {})
            for mid in list(store):
                if mid not in ids_vivos:
                    store.pop(mid, None)
    return bool(ao_vivo)


# ==========================================================================
# 7b) CHANCE DE GOL AO VIVO
# ==========================================================================

NOMES_FINALIZACOES = {"totalShots", "shots"}
NOMES_NO_ALVO = {"shotsOnTarget", "shotsOnGoal"}
NOMES_CANTOS = {"wonCorners", "cornerKicks"}


def minuto_num(relogio: str) -> Optional[int]:
    nums = [int(x) for x in re.findall(r"\d+", relogio or "")]
    return sum(nums[:2]) if nums else None


def buscar_stats(j: Jogo) -> Optional[dict]:
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
                val = _int(st.get("displayValue", st.get("value")))
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
    for k, v in list(ids_sinal.items()):
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
            gol_min[j.id] = m
        total_ant[j.id] = j.total
        if j.descricao.strip().lower() == "halftime":
            continue
        info = ids_sinal.get(j.id)
        if info and info["n"] >= PRESS_MAX_POR_JOGO:
            continue
        if m <= PRESS_MAX_MIN:
            candidatos.append((j, m))
    if not candidatos:
        return

    with ThreadPoolExecutor(max_workers=6) as ex:
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
            enviar_telegram("⚠️ A ESPN não trouxe finalizações/cantos para um jogo ao vivo.")
            continue

        if not (PRESS_MIN_MIN <= m <= PRESS_MAX_MIN):
            continue
        if abs(j.gols_mandante - j.gols_visitante) > PRESS_DIF_MAX:
            continue
        ult = gol_min.get(j.id)
        if ult is not None and m - ult < PRESS_SEM_GOL:
            continue
        info = ids_sinal.get(j.id)
        if info and agora_ts - info["ts"] < PRESS_INTERVALO * 60:
            continue
        bases = [x for x in lista if x[0] <= m - (PRESS_JANELA - 2)]
        if bases:
            base = bases[-1]
            if m - base[0] > PRESS_JANELA + 5:
                continue
        elif m <= PRESS_JANELA:
            base = (0, 0, 0, 0, 0, 0, 0)
        else:
            continue
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
        registrar_sinal(estado, j, "pressao", f"{m}'")
        enviar_telegram(msg_pressao(j, m, d, pressiona, max(1, m - base[0])))


# ==========================================================================
# 8) RELATÓRIO DIÁRIO
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
        return

    texto, resolvidos = montar_relatorio(estado, agora_.strftime("%d/%m/%Y"))
    if enviar_telegram(texto):
        for s in resolvidos:
            s["reportado"] = True
        estado["ultimo_relatorio"] = hoje
        estado["sinais"] = [s for s in estado["sinais"]
                            if s["status"] == "pendente" or not s["reportado"]]
        salvar_estado(estado)
        log.info("Relatório diário enviado.")


# ==========================================================================
# 8b) PRÉ-LIVE
# ==========================================================================

def gols_1t(competidor: dict) -> Optional[int]:
    ls = competidor.get("linescores") or []
    if len(ls) < 2:
        return None
    for item in ls:
        if item.get("period") == 1:
            return _int(item.get("value"))
    return _int(ls[0].get("value"))


def gols_1t_por_lances(comp: dict, casa: dict, fora: dict) -> Optional[int]:
    lances = comp.get("details")
    if lances is None:
        return None
    gols = [d for d in lances if d.get("scoringPlay") and not d.get("shootout")]
    if len(gols) != _int(casa.get("score")) + _int(fora.get("score")):
        return None
    n = 0
    for d in gols:
        nums = re.findall(r"\d+", (d.get("clock") or {}).get("displayValue", "") or "")
        if not nums:
            return None
        if int(nums[0]) <= 45:
            n += 1
    return n


def parse_passado(ev: dict) -> Optional[tuple]:
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
        if casa.get("score") is None or fora.get("score") is None:
            return None
        hc, hf = gols_1t(casa), gols_1t(fora)
        if hc is not None and hf is not None:
            total_1t = hc + hf
        else:
            total_1t = gols_1t_por_lances(comp, casa, fora)
        total_ft = _int(casa.get("score")) + _int(fora.get("score"))
        return (ev.get("date", ""), str(casa["team"]["id"]), str(fora["team"]["id"]), total_1t, total_ft)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def carregar_historico(slug: str, memoria: dict) -> Optional[dict]:
    hoje_dt = agora().date()
    hoje = hoje_dt.strftime("%Y%m%d")
    cache = memoria.setdefault("hist", {})
    if slug in cache and cache[slug][0] == hoje:
        return cache[slug][1]

    if get_espn(f"{slug}/scoreboard") is None:
        memoria.setdefault("hist_info", {})[slug] = (0, 0, 0, 1, memoria.get("span", 10))
        return None

    limite_antigo = hoje_dt - timedelta(days=HIST_DIAS)
    orcamento = 70
    consultas = 0
    falhas_seguidas = 0
    eventos: list = []
    cursor = hoje_dt
    while cursor >= limite_antigo and orcamento > 0 and falhas_seguidas < 5:
        span = memoria.get("span", 10)
        ini = max(cursor - timedelta(days=span - 1), limite_antigo)
        datas = ini.strftime("%Y%m%d") if ini == cursor else f"{ini:%Y%m%d}-{cursor:%Y%m%d}"
        variantes = ("&limit=300", "") if memoria.get("usar_limit", True) else ("",)
        evs = None
        for k, sufixo in enumerate(variantes):
            if orcamento <= 0:
                break
            orcamento -= 1
            consultas += 1
            dados = get_espn(f"{slug}/scoreboard?dates={datas}{sufixo}")
            if dados is not None:
                evs = dados.get("events", [])
                if sufixo == "" and k > 0:
                    memoria["usar_limit"] = False
                break
        if evs is None:
            if span > 1:
                memoria["span"] = max(1, span // 2)
                continue
            falhas_seguidas += 1
            cursor -= timedelta(days=1)
            continue
        falhas_seguidas = 0
        eventos.extend(evs)
        cursor = ini - timedelta(days=1)
        time.sleep(PAUSA_ENTRE_REQUISICOES)

    brutos_1t: dict = {}
    brutos_ft: dict = {}
    vistos = set()
    finalizados = com_1t = 0
    for ev in eventos:
        if ev.get("id") in vistos:
            continue
        vistos.add(ev.get("id"))
        p = parse_passado(ev)
        if not p:
            continue
        finalizados += 1
        data, id_casa, id_fora, total_1t, total_ft = p
        for tid in (id_casa, id_fora):
            brutos_ft.setdefault(tid, []).append((data, total_ft))
            if total_1t is not None:
                brutos_1t.setdefault(tid, []).append((data, total_1t))
        if total_1t is not None:
            com_1t += 1

    memoria.setdefault("hist_info", {})[slug] = (len(vistos), finalizados, com_1t, consultas,
                                                 memoria.get("span", 10))
    if not eventos:
        return None

    def ordenar(brutos: dict) -> dict:
        return {tid: sorted(lista, reverse=True) for tid, lista in brutos.items()}

    resultado = {"1t": ordenar(brutos_1t), "ft": ordenar(brutos_ft)}
    cache[slug] = (hoje, resultado)
    return resultado


def historico_para(memoria: dict, ids: set) -> tuple:
    hoje = agora().strftime("%Y%m%d")
    g = memoria.get("hist_merge")
    if not g or g["dia"] != hoje:
        g = {"dia": hoje, "1t": {}, "ft": {}, "slugs": set()}
        memoria["hist_merge"] = g

    def tot(chave: str, tid: str) -> list:
        return [x for _, x in sorted(g[chave].get(tid, []), reverse=True)[:HIST_JOGOS]]

    def coberto() -> bool:
        return all(len(tot("1t", i)) >= MIN_JOGOS_HIST or len(tot("ft", i)) >= MIN_JOGOS_HIST
                   for i in ids)

    for slug in LIGAS + [x for x in HIST_EXTRA if x not in LIGAS]:
        if coberto():
            break
        if slug in g["slugs"]:
            continue
        h = carregar_historico(slug, memoria)
        if not h:
            continue
        g["slugs"].add(slug)
        for chave in ("1t", "ft"):
            for tid, lista in h[chave].items():
                g[chave].setdefault(tid, []).extend(lista)
    return ({i: tot("1t", i) for i in ids}, {i: tot("ft", i) for i in ids})


def inicio_dt(j: Jogo) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(j.inicio.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=None).astimezone()
    except (ValueError, AttributeError):
        return None


def hora_local(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(TZ) if TZ else dt.astimezone()
        return dt.strftime("%H:%M")
    except (ValueError, AttributeError):
        return "--:--"


def montar_prelive(slug: str, memoria: dict, dia: Optional[str] = None) -> Optional[str]:
    diag = memoria.setdefault("diag", {})
    alvo = dia or agora().strftime("%Y%m%d")
    dados = get_espn(f"{slug}/scoreboard?dates={alvo}")
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
    agora_utc = datetime.now().astimezone()
    do_dia, jogos = [], []
    for j in todos:
        dt = inicio_dt(j)
        if dt is not None:
            local = dt.astimezone(TZ) if TZ else dt.astimezone()
            if local.strftime("%Y%m%d") != alvo:
                continue
        do_dia.append(j)
        if j.estado == "pre" and (dt is None or dt > agora_utc):
            jogos.append(j)
    diag[slug] = (rotulo, len(do_dia), len(jogos), True)
    if not jogos:
        return None
    sufixo = " (amanhã)" if dia else ""

    ids = {j.id_mandante for j in jogos} | {j.id_visitante for j in jogos}
    h1, hft = historico_para(memoria, ids)

    linhas, sem_dados = [], 0
    for j in jogos:
        a1, b1 = h1.get(j.id_mandante, []), h1.get(j.id_visitante, [])
        if len(a1) >= MIN_JOGOS_HIST and len(b1) >= MIN_JOGOS_HIST:
            ma, mb = sum(a1) / len(a1), sum(b1) / len(b1)
            pa = sum(1 for x in a1 if x >= 1) / len(a1) * 100
            pb = sum(1 for x in b1 if x >= 1) / len(b1) * 100
            linhas.append(((ma + mb) / 2, (pa + pb) / 2, j, ma, mb, pa, pb, False))
            continue
        af, bf = hft.get(j.id_mandante, []), hft.get(j.id_visitante, [])
        if len(af) >= MIN_JOGOS_HIST and len(bf) >= MIN_JOGOS_HIST:
            fa, fb = sum(af) / len(af), sum(bf) / len(bf)
            linhas.append(((fa + fb) / 2 * FATOR_1T, 0.0, j, fa, fb, None, None, True))
            continue
        sem_dados += 1
    linhas.sort(key=lambda x: (x[0], x[1]), reverse=True)

    out = [f"🔮 <b>PRÉ-LIVE — {esc(rotulo)}{sufixo}</b>", "Chance de gol no 1º tempo", ""]
    if not linhas:
        out.append("Sem histórico suficiente para ranquear. Jogos por começar:")
        for j in jogos[:10]:
            out.append(f"• {esc(j.mandante)} x {esc(j.visitante)} · {hora_local(j.inicio)}")
        cobertos = sum(1 for i in ids if len(h1.get(i, [])) >= MIN_JOGOS_HIST
                       or len(hft.get(i, [])) >= MIN_JOGOS_HIST)
        out += ["", f"ℹ️ Diagnóstico: histórico suficiente para {cobertos} de {len(ids)} times."]
        return "\n".join(out)

    estimado = False
    for i, (media, pct, j, ma, mb, pa, pb, est) in enumerate(linhas[:PRELIVE_TOP], 1):
        out.append(f"<b>{i}. {esc(j.mandante)} x {esc(j.visitante)}</b> · {hora_local(j.inicio)}")
        if est:
            estimado = True
            out.append(f"   Gols por jogo: {ma:.1f} | {mb:.1f} → ≈ <b>{media:.2f}</b> no 1T")
        else:
            out.append(f"   Média de gols no 1T: <b>{media:.2f}</b> ({ma:.1f} | {mb:.1f})")
            out.append(f"   Jogos com gol no 1T: {pa:.0f}% | {pb:.0f}%")
    out.append("")
    nota = f"ℹ️ Base: últimos {HIST_JOGOS} jogos de cada time."
    if estimado:
        nota += " ≈ = estimado pelos gols do jogo todo."
    if sem_dados:
        nota += f" ({sem_dados} jogo(s) sem dados suficientes)"
    out.append(nota)
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
            linhas += ["", "A ESPN não respondeu em nenhuma liga."]
        elif not dia:
            linhas += ["", "Dica: /prelive amanha mostra os jogos de amanhã."]
        enviar_telegram("\n".join(linhas))


def prelive_em_segundo_plano(memoria: dict, dia: Optional[str] = None) -> None:
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
    estado["ultimo_prelive"] = hoje
    salvar_estado(estado)
    prelive_em_segundo_plano(memoria)


def verificar_comandos(memoria: dict) -> None:
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
        if primeira:
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
# 9) LOOP PRINCIPAL & SERVIDORES DE SAÚDE
# ==========================================================================

def iniciar_servidor_saude() -> None:
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

        def log_message(self, *args):
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
        sys.exit("Configure TELEGRAM_TOKEN e CHAT_ID.")

    iniciar_servidor_saude()
    estado = carregar_estado()
    memoria = {"ultimo": {}, "cartoes": {}, "hist": {}, "offset": None}
    falhas = 0

    enviar_telegram(
        "🤖 <b>Bot iniciado!</b>\n"
        f"Ligas monitoradas: {len(LIGAS)}\n"
        "Alerta ao vivo: 🔥 CHANCE DE GOL (jogo todo)\n"
        "Envie /prelive no chat do bot para ver os jogos!"
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
            espera = min(120.0, INTERVALO_AO_VIVO * (2 ** falhas))
            log.exception("Erro na rodada (falha #%d). Nova tentativa em %.0fs.", falhas, espera)

        try:
            verificar_relatorio(estado)
        except Exception:
            log.exception("Erro ao gerar o relatório diário.")

        try:
            verificar_prelive_auto(estado, memoria)
        except Exception:
            log.exception("Erro no pré-live automático.")

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
