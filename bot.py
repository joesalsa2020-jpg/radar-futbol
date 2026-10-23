import requests
import numpy as np
import os
from scipy.stats import poisson
from datetime import datetime
from flask import Flask, redirect

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

app = Flask(__name__)

SCANNER_ACTIVO = True
ALERTAS_ENVIADAS = set() 
MEMORIA_EQUIPOS = {}

LIGAS_TOP = {
    "eng.1": "Premier League", "eng.3": "League One", "esp.1": "La Liga",
    "ita.1": "Serie A", "ger.1": "Bundesliga", "fra.1": "Ligue 1",
    "uefa.champions": "Champions League", "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League", "usa.1": "MLS", "mex.1": "Liga MX",
    "arg.1": "Liga Argentina", "bra.1": "Brasileirão", 
    "conmebol.libertadores": "Libertadores", "conmebol.sudamericana": "Sudamericana"
}

def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
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

def cuota_decimal_a_prob(cuota):
    if cuota <= 1.0: return 0.0
    return (1 / cuota) * 100

def buscar_cuota_infersports(equipo_local, equipo_visitante):
    try:
        r = requests.get("https://api.infersports.dev/v1/events/live", timeout=5)
        if r.status_code != 200: return None
        eventos = r.json().get('data', [])
        
        # Búsqueda inteligente por las dos primeras palabras del equipo
        palabra_local = " ".join(equipo_local.split()[:2]).lower()
        palabra_visitante = " ".join(equipo_visitante.split()[:2]).lower()
        
        event_id = None
        for ev in eventos:
            h_team = ev.get('home_team', {}).get('name', '').lower()
            a_team = ev.get('away_team', {}).get('name', '').lower()
            
            # Buscamos coincidencia (puede ser que ESPN diga "Barra FC" e InferSports "Barra SC")
            # Usamos la primera palabra para ser flexibles
            h_palabra = equipo_local.split()[0].lower()
            a_palabra = equipo_visitante.split()[0].lower()
            
            if h_palabra in h_team and a_palabra in a_team:
                event_id = ev['id']
                break
                
        if not event_id: return None
            
        r_odds = requests.get(f"https://api.infersports.dev/v1/events/{event_id}/odds?format=decimal", timeout=5)
        if r_odds.status_code != 200: return None
        
        odds_list = r_odds.json().get('odds', [])
        
        for odd in odds_list:
            if odd.get('market_type') == 'totals' and odd.get('period') == 'full_time' and odd.get('status') == 'open':
                linea = odd.get('line')
                precio_over = odd.get('prices', {}).get('over', 0)
                precio_under = odd.get('prices', {}).get('under', 0)
                
                if linea and precio_over > 1.0 and precio_under > 1.0:
                    return {
                        "linea": linea,
                        "cuota_over": precio_over,
                        "cuota_under": precio_under,
                        "prob_casa_over": cuota_decimal_a_prob(precio_over),
                        "prob_casa_under": cuota_decimal_a_prob(precio_under)
                    }
        return None
    except Exception:
        return None

def escanear_partidos():
    global SCANNER_ACTIVO
    if not SCANNER_ACTIVO:
        return "<h1>🛑 El escáner está pausado.</h1><br><a href='/start'><button style='padding:10px; font-size:16px;'>▶ Reanudar Escáner</button></a>"

    resultados_html = "<h1>Resultados del Escaneo en Vivo (Cuotas Gratis)</h1>"
    resultados_html += "<a href='/stop'><button style='padding:10px; font-size:16px; background-color:#ff4d4d; color:white; border:none; border-radius:5px;'>🛑 Detener Escáner</button></a><br><br>"
    
    partidos_vivos = 0
    debug_msgs = ""
    
    for liga_code, liga_nombre in LIGAS_TOP.items():
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
                
                # --- REGLA: Solo evaluar si pasó el minuto 55 ---
                if minuto < 55: 
                    debug_msgs += f"<p>⏳ [{liga_nombre}] {nombre} va en el minuto {minuto}. (Muy pronto).</p>"
                    continue
                
                comp = partido['competitions'][0]
                equipo_local = comp['competitors'][0]['team']['displayName']
                equipo_visitante = comp['competitors'][1]['team']['displayName']
                
                score_local = int(comp['competitors'][0].get('score', {}).get('displayValue', 0) if isinstance(comp['competitors'][0].get('score'), dict) else comp['competitors'][0].get('score', 0))
                score_visitante = int(comp['competitors'][1].get('score', {}).get('displayValue', 0) if isinstance(comp['competitors'][1].get('score'), dict) else comp['competitors'][1].get('score', 0))
                goles_totales = score_local + score_visitante
                
                # --- OBTENER CUOTAS REALES (INFERSPORTS) ---
                cuotas = buscar_cuota_infersports(equipo_local, equipo_visitante)
                if not cuotas: 
                    debug_msgs += f"<p>⚠️ [{liga_nombre}] {nombre} (Min {minuto}'). No se encontraron cuotas abiertas en InferSports.</p>"
                    continue 
                
                linea_real = cuotas['linea']
                
                url_summary = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/summary?event={event_id}"
                data = requests.get(url_summary, timeout=5).json()
                tiros, corners = 0, 0
                for eq_stats in data.get('boxscore', {}).get('teams', []):
                    for stat in eq_stats.get('statistics', []):
                        if stat['name'] == 'totalShots': tiros += int(stat['displayValue'])
                        elif stat['name'] == 'wonCorners': corners += int(stat['displayValue'])
                
                perfil_local = obtener_perfil_equipo(liga_code, comp['competitors'][0]['team']['id'])
                perfil_visitante = obtener_perfil_equipo(liga_code, comp['competitors'][1]['team']['id'])
                
                franja_actual = min(minuto // 15, 5)
                prop_franja = (15 - (minuto % 15)) / 15.0
                goles_esperados = sum((perfil_local[i] + perfil_visitante[i]) * (prop_franja if i == franja_actual else 1.0) for i in range(franja_actual, 6))
                
                fraccion = minuto / 90.0
                factor_ritmo = max(0.5, min(2.0, ((tiros / max(1, 25 * fraccion)) + (corners / max(1, 10 * fraccion))) / 2.0)) if minuto > 10 else 1.0
                
                goles_ajustados = goles_esperados * factor_ritmo
                goles_necesarios = int(np.floor(linea_real - goles_totales)) + 1
                
                prob_over_bot = 100.0 if goles_necesarios <= 0 else (1 - sum(poisson.pmf(i, goles_ajustados) for i in range(goles_necesarios))) * 100
                prob_under_bot = 100.0 - prob_over_bot
                
                # --- LÓGICA DE VALOR ---
                valor_over = prob_over_bot - cuotas['prob_casa_over']
                valor_under = prob_under_bot - cuotas['prob_casa_under']
                
                if valor_over > 5 or valor_under > 5:
                    recomendacion = "OVER" if valor_over > valor_under else "UNDER"
                    cuota_recomendada = cuotas['cuota_over'] if recomendacion == "OVER" else cuotas['cuota_under']
                    prob_casa = cuotas['prob_casa_over'] if recomendacion == "OVER" else cuotas['prob_casa_under']
                    prob_bot = prob_over_bot if recomendacion == "OVER" else prob_under_bot
                    
                    clave_alerta = f"{event_id}_{recomendacion}_{linea_real}"
                    
                    if clave_alerta not in ALERTAS_ENVIADAS:
                        ALERTAS_ENVIADAS.add(clave_alerta)
                        mensaje = (
                            f"🚨 *¡VALOR ENCONTRADO!* 🚨\n"
                            f"⚽ *[{liga_nombre}]* {nombre}\n"
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
                else:
                    debug_msgs += f"<p>📉 [{liga_nombre}] {nombre} (Min {minuto}'). Cuotas encontradas, pero sin valor.</p>"
                        
        except Exception:
            continue
            
    if partidos_vivos == 0:
        resultados_html += "<p>💤 No hay partidos en vivo en las ligas seleccionadas.</p>"
    else:
        resultados_html += f"<p>✅ Se encontraron {partidos_vivos} partidos en vivo.</p>"
        resultados_html += f"<div style='background-color:#f0f0f0; padding:10px; color:black;'>{debug_msgs}</div>"
        
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
