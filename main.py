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

HEADERS = {
    'x-apisports-key': API_KEY
}

# As 11 Principais Ligas
LIGAS_SELECIONADAS = [
    39,   # Premier League
    140,  # La Liga
    135,  # Serie A
    78,   # Bundesliga
    61,   # Ligue 1
    2,    # Champions League
    3,    # Europa League
    71,   # Brasileirão Série A
    72,   # Brasileirão Série B
    13,   # Copa do Brasil
    11    # Copa Libertadores
]

# Parâmetros de Alerta
PRESS_MIN_MIN = int(os.getenv("PRESS_MIN_MIN", "1"))
PRESS_MAX_MIN = int(os.getenv("PRESS_MAX_MIN", "130"))
PRESS_MAX_POR_JOGO = int(os.getenv("PRESS_MAX_POR_JOGO", "2"))
PRESS_INTERVALO = int(os.getenv("PRESS_INTERVALO", "10"))
PRESS_FINALIZACOES = int(os.getenv("PRESS_FINALIZACOES", "4"))
PRESS_NO_ALVO = int(os.getenv("PRESS_NO_ALVO", "2"))
PRESS_CANTOS = int(os.getenv("PRESS_CANTOS", "2"))
PRESS_DIF_MAX = int(os.getenv("PRESS_DIF_MAX", "1"))

ALERTAS_FUTEBOL = {}
REGISTRO_ENTRADAS = []  # Para o relatório diário de Green/Red

# ==========================================
# 2. SERVIDOR FLASK (Render Health Check)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de Futebol Completo (Pré-Live, HT, BTTS e Ao Vivo) rodando 100%!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ERRO TELEGRAM] {e}")

# ==========================================
# 3. ANÁLISE AO VIVO (PRESSÃO, HT E BTTS)
# ==========================================
def processar_futebol():
    agora = time.time()

    for liga_id in LIGAS_SELECIONADAS:
        url = f"{FOOTBALL_URL}/fixtures?live=all&league={liga_id}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                continue
            jogos = response.json().get("response", [])
        except Exception as e:
            print(f"[ERRO REQUISIÇÃO LIGA {liga_id}] {e}")
            continue

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

            # Buscar estatísticas avançadas da partida
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

                # Indicadores analíticos para HT e BTTS com base na pressão atual
                prob_ht = "🔥 Alta (Forte pressão ofensiva)" if tempo_decorrido <= 45 else "⏳ Jogo avançado (Foco em Over Final/Cantos)"
                btts_status = "✅ Favorável (Ambos criando volume)" if (gols_casa == 0 and gols_fora == 0) or (total_chutes >= 8) else "⚠️ Atenção ao contexto"

                msg = (
                    f"⚽ <b>OPORTUNIDADE AO VIVO (SNIPER)</b> 🔥\n\n"
                    f"🏆 <b>Liga:</b> {league}\n"
                    f"⚔️ <b>Jogo:</b> {casa} {gols_casa} x {gols_fora} {fora}\n"
                    f"⏱️ <b>Tempo:</b> {tempo_decorrido}' min\n\n"
                    f"📊 <b>Estatísticas de Pressão:</b>\n"
                    f"🎯 Remates: {total_chutes} | No Alvo: {chutes_no_alvo}\n"
                    f"🚩 Cantos: {escanteios}\n\n"
                    f"💡 <b>Análises de Mercado:</b>\n"
                    f"• <b>Tendência Gol HT:</b> {prob_ht}\n"
                    f"• <b>Ambos Marcam (BTTS):</b> {btts_status}\n\n"
                    f"🚀 <i>Entrada monitorada pelo sistema!</i>"
                )

                enviar_telegram(msg)
                
                # Regista a entrada para o relatório diário
                REGISTRO_ENTRADAS.append({
                    "jogo": f"{casa} vs {fora}",
                    "liga": league,
                    "tempo": f"{tempo_decorrido}'",
                    "status": "Pendente"
                })

                ALERTAS_FUTEBOL[fixture_id] = {'count': historico['count'] + 1, 'last_time': agora}
                time.sleep(1)

# ==========================================
# 4. RELATÓRIO DIÁRIO DE DESEMPENHO (GREEN/RED)
# ==========================================
def relatorio_diario_loop():
    while True:
        agora = datetime.now(timezone(timedelta(hours=-3)))
        # Envia o relatório resumo ao final do dia (ex: às 23:59)
        if agora.hour == 23 and agora.minute == 59:
            total_entradas = len(REGISTRO_ENTRADAS)
            msg_relatorio = (
                f"📊 <b>RELATÓRIO DIÁRIO DE PERFORMANCE</b> 📈\n\n"
                f"📅 <b>Data:</b> {agora.strftime('%d/%m/%Y')}\n"
                f"🎯 Total de Alertas/Entradas Disparadas: {total_entradas}\n\n"
                f"💡 <i>Confira a tua planilha para computar os Greens e Reds finais de hoje!</i>"
            )
            enviar_telegram(msg_relatorio)
            REGISTRO_ENTRADAS.clear()  # Limpa para o dia seguinte
            time.sleep(70)  # Evita duplicar no mesmo minuto
        time.sleep(30)

# ==========================================
# 5. LOOPS PRINCIPAIS
# ==========================================
def loop_futebol():
    print("[BOT] Monitor Completo de Futebol iniciado...")
    while True:
        try:
            processar_futebol()
        except Exception as e:
            print(f"[ERRO LOOP FUTEBOL] {e}")
        time.sleep(60)

if __name__ == "__main__":
    t_flask = Thread(target=run_flask)
    t_flask.daemon = True
    t_flask.start()

    t_fut = Thread(target=loop_futebol)
    t_fut.daemon = True
    t_fut.start()

    t_rel = Thread(target=relatorio_diario_loop)
    t_rel.daemon = True
    t_rel.start()

    while True:
        time.sleep(3600)
