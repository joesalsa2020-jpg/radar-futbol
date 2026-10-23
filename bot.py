import requests
import numpy as np
import os
from scipy.stats import poisson
from datetime import datetime
from flask import Flask, redirect

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

app = Flask(__name__)

# Variables globales
SCANNER_ACTIVO = True
ALERTAS_ENVIADAS = set() # Memoria anti-spam
MEMORIA_EQUIPOS = {}

# Mapeo de ligas de ESPN a The Odds API
LIGAS_TOP = {
    "eng.1": {"nombre": "Premier League", "odds_key": "soccer_epl"},
    "esp.1": {"nombre": "La Liga", "odds_key": "soccer_spain_la_liga"},
    "ita.1": {"nombre": "Serie A", "odds_key": "soccer_italy_serie_a"},
    "ger.1": {"nombre": "Bundesliga", "odds_key": "soccer_germany_bundesliga"},
    "fra.1": {"nombre": "Ligue 1", "odds_key": "soccer_france_ligue_one"},
    "uefa.champions": {"nombre": "Champions League", "odds_key": "soccer_uefa_champs_league"},
    "usa.1": {"nombre": "MLS", "odds_key": "soccer_usa_mls"},
    "mex.1": {"nombre": "Liga MX", "odds_key": "soccer_mexico_ligamx"},
}

def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def obtener_perfil_equipo(liga, team_id):
    if team_id in MEMORIA_EQUIPOS: return MEMORIA_EQUIPOS[team_id]
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/teams/{team_id}/schedule"
    distribucion = [0.0] * 6
    total_goles = 0
    try:
        r = requests.get(url, timeout=5)
        eventos = r.json().get('events', [])
        partidos_terminados = [e for e in eventos if e.get('competitions', [{}])[0].get('status', {}).get('type', {}).get('completed')]
        for ev in partidos_terminados[-5:]:
            url_summary = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/summary?event={ev['id']}"
            data = requests.get(url_summary, timeout=5).json()
            for k_ev in data.get('keyEvents', []):
                if "Goal" in k_ev.get('type', {}).get('text', '') or "Penalty" in k_ev.get('type', {}).get('text', ''):
                    if str(k_ev.get('team', {}).get('id')) == str(team_id):
                        minuto = int(''.join(filter(str.isdigit, k_ev.get('clock', {}).get('displayValue', '0').split('+')[0])))
                        distribucion[min(minuto // 15, 5)] += 1
                        total_goles += 1
    except Exception: pass
    
    if total_goles > 0:
        distribucion = [g / total_goles for g in distribucion]
    else:
        distribucion = [0.15, 0.16, 0.18, 0.18, 0.17, 0.16]
    MEMORIA_EQUIPOS[team_id] = distribucion
    return distribucion

def convertir_cuota_a_probabilidad(cuota_americana):
    """Convierte la cuota americana (-110, +150) a un porcentaje de probabilidad."""
    if cuota_americana < 0:
        return abs(cuota_americana) / (abs(cuota_americana) + 100) * 100
    else:
        return 100 / (cuota_americana + 100) * 100

def obtener_cuota_real(liga_odds_key, equipo_local, equipo_visitante):
    """Busca el partido en The Odds API y devuelve la línea y cuotas reales."""
    if not ODDS_API_KEY: return None
    
    url = f"https://api.the-odds-api.com/v4/sports/{liga_odds_key}/odds/"
    params = {"apiKey": ODDS_API_KEY, "regions": "us,eu", "markets": "totals", "oddsFormat": "american"}
    
    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code != 200: return None
        
        partidos = r.json()
        # Buscamos el partido (comparando la primera palabra del nombre del equipo)
        palabra_local = equipo_local.split()[0].lower()
        palabra_visitante = equipo_visitante.split()[0].lower()
        
        for p in partidos:
            if palabra_local in p['home_team'].lower() and palabra_visitante in p['away_team'].lower():
                # Buscamos la casa de apuestas Pinnacle o la primera disponible
                for bookmaker in p['bookmakers']:
                    if bookmaker['key'] == 'pinnacle' or bookmaker['key'] == 'draftkings':
                        for market in bookmaker['markets']:
                            if market['key'] == 'totals':
                                # El primer outcome suele ser Over, el segundo Under
                                over = market['outcomes'][0]
                                under = market['outcomes'][1]
                                return {
                                    "linea": over['point'],
                                    "prob_over_casa": convertir_cuota_a_probabilidad(over['price']),
                                    "prob_under_casa": convertir_cuota_a_probabilidad(under['price']),
                                    "cuota_over": over['price'],
                                    "cuota_under": under['price']
                                }
        return None
    except Exception:
        return None

def escanear_partidos():
    global SCANNER_ACTIVO
    if not SCANNER_ACTIVO:
        return "<h1>🛑 El escáner está pausado.</h1><br><a href='/start'><button style='padding:10px; font-size:16px;'>▶ Reanudar Escáner</button></a>"

    resultados_html = "<h1>Resultados del Escaneo en Vivo</h1>"
    resultados_html += "<a href='/stop'><button style='padding:10px; font-size:16px; background-color:#ff4d4d; color:white; border:none; border-radius:5px;'>🛑 Detener Escáner</button></a><br><br>"
    
    partidos_vivos = 0
    
    for liga_code, liga_info in LIGAS_TOP.items():
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/scoreboard"
        try:
            r = requests.get(url, timeout=5)
            eventos = r.json().get('events', [])
            for partido in eventos:
                if partido['status']['type']['state'] != 'in': continue
                
                partidos_vivos += 1
                event_id = partido['id']
                nombre = partido['name']
                minuto_str = partido['status'].get('displayClock', '0')
                minuto = int(minuto_str.replace("'", "")) if "'" in minuto_str else int(minuto_str.split(":")[0]) if ":" in minuto_str else 45
                
                # --- REGLA DE ORO: Solo evaluar si pasó el minuto 55 ---
                if minuto < 55: continue
                
                comp = partido['competitions'][0]
                equipo_local = comp['competitors'][0]['team']['displayName']
                equipo_visitante = comp['competitors'][1]['team']['displayName']
                
                score_local = int(comp['competitors'][0].get('score', {}).get('displayValue', 0) if isinstance(comp['competitors'][0].get('score'), dict) else comp['competitors'][0].get('score', 0))
                score_visitante = int(comp['competitors'][1].get('score', {}).get('displayValue', 0) if isinstance(comp['competitors'][1].get('score'), dict) else comp['competitors'][1].get('score', 0))
                goles_totales = score_local + score_visitante
                
                # --- OBTENER CUOTAS REALES ---
                cuotas = obtener_cuota_real(liga_info['odds_key'], equipo_local, equipo_visitante)
                if not cuotas: continue # Si no hay cuotas, saltamos a otro partido
                
                linea_real = cuotas['linea']
                
                # Obtenemos estadísticas en vivo
                url_summary = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/summary?event={event_id}"
                data = requests.get(url_summary, timeout=5).json()
                tiros, corners = 0, 0
                for eq_stats in data.get('boxscore', {}).get('teams', []):
                    for stat in eq_stats.get('statistics', []):
                        if stat['name'] == 'totalShots': tiros += int(stat['displayValue'])
                        elif stat['name'] == 'wonCorners': corners += int(stat['displayValue'])
                
                perfil_local = obtener_perfil_equipo(liga_code, comp['competitors'][0]['team']['id'])
                perfil_visitante = obtener_perfil_equipo(liga_code, comp['competitors'][1]['team']['id'])
                
                # Matemáticas (Poisson)
                franja_actual = min(minuto // 15, 5)
                prop_franja = (15 - (minuto % 15)) / 15.0
                goles_esperados = sum((perfil_local[i] + perfil_visitante[i]) * (prop_franja if i == franja_actual else 1.0) for i in range(franja_actual, 6))
                
                fraccion = minuto / 90.0
                factor_ritmo = max(0.5, min(2.0, ((tiros / max(1, 25 * fraccion)) + (corners / max(1, 10 * fraccion))) / 2.0)) if minuto > 10 else 1.0
                
                goles_ajustados = goles_esperados * factor_ritmo
                goles_necesarios = int(np.floor(linea_real - goles_totales)) + 1
                
                prob_over_bot = 100.0 if goles_necesarios <= 0 else (1 - sum(poisson.pmf(i, goles_ajustados) for i in range(goles_necesarios))) * 100
                prob_under_bot = 100.0 - prob_over_bot
                
                # --- LÓGICA DE VALOR (Value Bet) ---
                # Solo alertamos si nuestra probabilidad es mayor a la de la casa por un 5%
                valor_over = prob_over_bot - cuotas['prob_over_casa']
                valor_under = prob_under_bot - cuotas['prob_under_casa']
                
                if valor_over > 5 or valor_under > 5:
                    recomendacion = "OVER" if valor_over > valor_under else "UNDER"
                    cuota_recomendada = cuotas['cuota_over'] if recomendacion == "OVER" else cuotas['cuota_under']
                    prob_casa = cuotas['prob_over_casa'] if recomendacion == "OVER" else cuotas['prob_under_casa']
                    prob_bot = prob_over_bot if recomendacion == "OVER" else prob_under_bot
                    
                    clave_alerta = f"{event_id}_{recomendacion}_{linea_real}"
                    
                    if clave_alerta not in ALERTAS_ENVIADAS:
                        ALERTAS_ENVIADAS.add(clave_alerta)
                        mensaje = (
                            f"🚨 *¡VALOR ENCONTRADO!* 🚨\n"
                            f"⚽ *[{liga_info['nombre']}]* {nombre}\n"
                            f"⏱️ Minuto: {minuto}'\n"
                            f"📊 Marcador: {score_local} - {score_visitante}\n"
                            f"📈 Stats: {tiros} tiros | {corners} córners\n"
                            f"⚙️ Ritmo: {factor_ritmo:.2f}x\n"
                            f"🎯 Línea Real: {linea_real}\n"
                            f"💰 Cuota {recomendacion}: {cuota_recomendada} (Casa: {prob_casa:.1f}% vs Bot: {prob_bot:.1f}%)\n"
                            f"🔥 *RECOMENDACIÓN: {recomendacion}*"
                        )
                        resultados_html += f"<p style='color:green;'><b>{mensaje.replace(chr(10), '<br>')}</b></p><hr>"
                        enviar_alerta_telegram(mensaje)
                        
        except Exception:
            continue
            
    if partidos_vivos == 0:
        resultados_html += "<p>💤 No hay partidos en vivo en este momento.</p>"
    else:
        resultados_html += "<p>✅ Escaneo terminado. Si no hay alertas arriba, es porque no hay valor en las cuotas actuales.</p>"
        
    return resultados_html

@app.route('/')
def home():
    return escanear_partidos()

@app.route('/stop')
def stop():
    global SCANNER_ACTIVO
    SCANNER_ACTIVO = False
    return redirect('/')

@app.route('/start')
def start():
    global SCANNER_ACTIVO
    SCANNER_ACTIVO = True
    return redirect('/')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
