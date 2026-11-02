import os
import time
import requests
from flask import Flask
from threading import Thread
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("API_FOOTBALL_KEY")

FOOTBALL_URL = "https://v3.football.api-sports.io"
BASKETBALL_URL = "https://v1.basketball.api-sports.io"

HEADERS = {
    'x-apisports-key': API_KEY
}

NBA_LEAGUE_ID = 12
SEASON_NBA = "2026"

# Parâmetros de Futebol
ALERTA_PRESSAO = os.getenv("ALERTA_PRESSAO", "1") == "1"
PRESS_MIN_MIN = int(os.getenv("PRESS_MIN_MIN", "1"))
PRESS_MAX_MIN = int(os.getenv("PRESS_MAX_MIN", "130"))
PRESS_MAX_POR_JOGO = int(os.getenv("PRESS_MAX_POR_JOGO", "2"))
PRESS_INTERVALO = int(os.getenv("PRESS_INTERVALO", "10"))
PRESS_FINALIZACOES = int(os.getenv("PRESS_FINALIZACOES", "4"))
PRESS_NO_ALVO = int(os.getenv("PRESS_NO_ALVO", "2"))
PRESS_CANTOS = int(os.getenv("PRESS_CANTOS", "2"))
PRESS_DIF_MAX = int(os.getenv("PRESS_DIF_MAX", "1"))

ALERTAS_FUTEBOL = {}
JOGOS_NBA_ENVIADOS = set()

# ==========================================
# 2. SERVIDOR FLASK (Health Check para Render)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Futebol (Ao Vivo) + Basquete Pré-Live (NBA com Comando) rodando 100%!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def enviar_telegram(mensagem, chat_id=None):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target_chat:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat, "text": mensagem, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")

# ==========================================
# 3. LÓGICA DE FUTEBOL (AO VIVO)
# ==========================================
def processar_futebol():
    if not ALERTA_PRESSAO:
        return

    url = f"{FOOTBALL_URL}/fixtures?live=all"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return
        jogos = response.json().get("response", [])
    except Exception as e:
        print(f"[ERRO REQUISIÇÃO FUTEBOL] {e}")
        return

    agora = time.time()

    for jogo in jogos:
        fixture = jogo.get("fixture", {})
        fixture_id = fixture.get("id")
        status = fixture.get("status", {})
        tempo_decorrido = status.get("elapsed", 0)

        if not tempo_decorrido or tempo_decorrido < PRESS_MIN_MIN or tempo_decorrido > PRESS_MAX_MIN:
            continue

        goals = jogo.get("goals", {})
        gols_casa = goals.get("home") or 0
        gols_fora = goals.get("away") or 0
        if abs(gols_casa - gols_fora) > PRESS_DIF_MAX:
            continue

        historico = ALERTAS_FUTEBOL.get(fixture_id, {'count': 0, 'last_time': 0})
        if historico['count'] >= PRESS_MAX_POR_JOGO:
            continue
        if agora - historico['last_time'] < (PRESS_INTERVALO * 60):
            continue

        stats_url = f"{FOOTBALL_URL}/fixtures/statistics?fixture={fixture_id}"
        try:
            stats_resp = requests.get(stats_url, headers=HEADERS, timeout=10)
            stats = stats_resp.json().get("response", []) if stats_resp.status_code == 200 else []
        except:
            stats = []

        if not stats:
            continue

        total_chutes, chutes_no_alvo, escanteios = 0, 0, 0
        for team_stat in stats:
            for stat in team_stat.get("statistics", []):
                val = stat.get("value")
                if val is not None and isinstance(val, (int, str)):
                    val_str = str(val).replace("%", "")
                    if val_str.isdigit():
                        if stat.get("type") == "Total Shots":
                            total_chutes += int(val_str)
                        elif stat.get("type") == "Shots on Goal":
                            chutes_no_alvo += int(val_str)
                        elif stat.get("type") == "Corner Kicks":
                            escanteios += int(val_str)

        if total_chutes >= PRESS_FINALIZACOES and chutes_no_alvo >= PRESS_NO_ALVO and escanteios >= PRESS_CANTOS:
            teams = jogo.get("teams", {})
            casa = teams.get("home", {}).get("name", "Casa")
            fora = teams.get("away", {}).get("name", "Fora")
            league = jogo.get("league", {}).get("name", "Liga")

            msg = (
                f"⚽ <b>CHANCE DE GOL (FUTEBOL)</b> 🔥\n\n"
                f"🏆 <b>Liga:</b> {league}\n"
                f"⚔️ <b>Jogo:</b> {casa} {gols_casa} x {gols_fora} {fora}\n"
                f"⏱️ <b>Tempo:</b> {tempo_decorrido}' min\n\n"
                f"📊 <b>Estatísticas:</b>\n"
                f"🎯 Remates: {total_chutes} | No Alvo: {chutes_no_alvo}\n"
                f"🚩 Cantos: {escanteios}\n\n"
                f"💡 <i>Pressão alta confirmada! Olho no mercado de Over.</i>"
            )

            enviar_telegram(msg)
            ALERTAS_FUTEBOL[fixture_id] = {'count': historico['count'] + 1, 'last_time': agora}
            time.sleep(1)

# ==========================================
# 4. LÓGICA DE BASQUETE PRÉ-LIVE (NBA)
# ==========================================
def buscar_jogos_nba():
    hoje = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    url = f"{BASKETBALL_URL}/games?league={NBA_LEAGUE_ID}&season={SEASON_NBA}&date={hoje}"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return "❌ Erro ao consultar a API de Basquete."
        jogos = response.json().get("response", [])
    except Exception as e:
        return f"[ERRO REQUISIÇÃO NBA] {e}"

    if not jogos:
        return f"🏀 Não há jogos da NBA agendados para hoje ({hoje})."

    texto = f"🏀 <b>JOGOS DA NBA PARA HOJE ({hoje})</b> 🇺🇸\n\n"
    for jogo in jogos:
        teams = jogo.get("teams", {})
        casa = teams.get("home", {}).get("name", "Casa")
        fora = teams.get("away", {}).get("name", "Fora")
        data_utc = jogo.get("date", "")
        
        try:
            dt_obj = datetime.fromisoformat(data_utc.replace("Z", "+00:00"))
            hora_local = dt_obj.astimezone(timezone(timedelta(hours=-3))).strftime("%H:%M")
        except:
            hora_local = "Em breve"

        texto += f"⚔️ <b>{casa} vs {fora}</b>\n⏰ Horário: {hora_local} (BR)\n\n"

    texto += "💡 <i>Prepare as suas análises de linhas de pontos!</i>"
    return texto

def loop_basquete_prelive():
    print("[BOT] Monitor de Basquete Pré-Live (NBA) iniciado...")
    while True:
        try:
            hoje = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
            url = f"{BASKETBALL_URL}/games?league={NBA_LEAGUE_ID}&season={SEASON_NBA}&date={hoje}"
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 200:
                jogos = response.json().get("response", [])
                for jogo in jogos:
                    game_id = jogo.get("id")
                    status = jogo.get("status", {})
                    if status.get("short", "") == "NS" and game_id not in JOGOS_NBA_ENVIADOS:
                        # Envio automático diário opcional
                        JOGOS_NBA_ENVIADOS.add(game_id)
        except Exception as e:
            print(f"[ERRO LOOP BASQUETE] {e}")
        time.sleep(3600)

# ==========================================
# 5. OUVINTE DE COMANDOS DO TELEGRAM (/nba)
# ==========================================
def ouvir_comandos_telegram():
    print("[BOT] Ouvinte de comandos do Telegram iniciado...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            resp = requests.get(url, timeout=35)
            if resp.status_code == 200:
                dados = resp.json()
                for update in dados.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    text = message.get("text", "").strip().lower()
                    chat_id = message.get("chat", {}).get("id")

                    # Se enviares /nba ou /basquete, o bot responde com a lista do dia
                    if text in ["/nba", "/basquete", "/prelive"]:
                        resposta_nba = buscar_jogos_nba()
                        enviar_telegram(resposta_nba, chat_id=chat_id)
        except Exception as e:
            print(f"[ERRO COMANDOS TELEGRAM] {e}")
        time.sleep(2)

# ==========================================
# 6. LOOPS PARALELOS (THREADS)
# ==========================================
if __name__ == "__main__":
    t_flask = Thread(target=run_flask)
    t_flask.daemon = True
    t_flask.start()

    t_fut = Thread(target=loop_futebol)
    t_fut.daemon = True
    t_fut.start()

    t_basq = Thread(target=loop_basquete_prelive)
    t_basq.daemon = True
    t_basq.start()

    t_cmd = Thread(target=ouvir_comandos_telegram)
    t_cmd.daemon = True
    t_cmd.start()

    while True:
        time.sleep(3600)
