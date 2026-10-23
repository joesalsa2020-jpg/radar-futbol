import requests
import numpy as np
import os
from scipy.stats import poisson
from datetime import datetime
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

app = Flask(__name__)

LIGAS_TOP = {
    "eng.1": "Premier League", "eng.3": "League One", "esp.1": "La Liga",
    "ita.1": "Serie A", "ger.1": "Bundesliga", "fra.1": "Ligue 1",
    "uefa.champions": "Champions League", "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League", "usa.1": "MLS", "mex.1": "Liga MX",
    "arg.1": "Liga Argentina", "bra.1": "Brasileirão", 
    "conmebol.libertadores": "Libertadores", "conmebol.sudamericana": "Sudamericana"
}

MEMORIA_EQUIPOS = {}

def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        pass

def obtener_perfil_equipo(liga, team_id):
    if team_id in MEMORIA_EQUIPOS:
        return MEMORIA_EQUIPOS[team_id]
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/teams/{team_id}/schedule"
    distribucion = [0.0] * 6
    total_goles_historicos = 0
    try:
        r = requests.get(url, timeout=5)
        eventos = r.json().get('events', [])
        partidos_terminados = [e for e in eventos if e.get('competitions', [{}])[0].get('status', {}).get('type', {}).get('completed')]
        ultimos_5 = partidos_terminados[-5:]
        for ev in ultimos_5:
            event_id = ev['id']
            url_summary = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/summary?event={event_id}"
            data = requests.get(url_summary, timeout=5).json()
            for key_event in data.get('keyEvents', []):
                tipo = key_event.get('type', {}).get('text', '')
                if "Goal" in tipo or "Penalty" in tipo:
                    team_id_gol = key_event.get('team', {}).get('id')
                    if str(team_id_gol) == str(team_id):
                        tiempo_str = key_event.get('clock', {}).get('displayValue', '0')
                        minuto_limpio = int(''.join(filter(str.isdigit, tiempo_str.split('+')[0])))
                        franja = min(minuto_limpio // 15, 5)
                        distribucion[franja] += 1
                        total_goles_historicos += 1
    except Exception:
        pass
    if total_goles_historicos > 0:
        distribucion = [g / total_goles_historicos for g in distribucion]
    else:
        distribucion = [0.15, 0.16, 0.18, 0.18, 0.17, 0.16]
    MEMORIA_EQUIPOS[team_id] = distribucion
    return distribucion

def escanear_partidos():
    resultados_html = "<h2>Resultados del Escaneo en Vivo</h2>"
    alertas_encontradas = 0
    partidos_vivos = 0
    
    for liga_code, liga_nombre in LIGAS_TOP.items():
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/scoreboard"
        try:
            r = requests.get(url, timeout=5)
            eventos = r.json().get('events', [])
            for partido in eventos:
                if partido['status']['type']['state'] != 'in':
                    continue
                
                partidos_vivos += 1
                event_id = partido['id']
                nombre = partido['name']
                minuto_str = partido['status'].get('displayClock', '0')
                minuto = int(minuto_str.replace("'", "")) if "'" in minuto_str else int(minuto_str.split(":")[0]) if ":" in minuto_str else 45
                
                comp = partido['competitions'][0]
                id_local = comp['competitors'][0]['team']['id']
                id_visitante = comp['competitors'][1]['team']['id']
                
                score_local_raw = comp['competitors'][0].get('score', '0')
                score_local = int(score_local_raw.get('displayValue', '0')) if isinstance(score_local_raw, dict) else int(score_local_raw)
                score_visitante_raw = comp['competitors'][1].get('score', '0')
                score_visitante = int(score_visitante_raw.get('displayValue', '0')) if isinstance(score_visitante_raw, dict) else int(score_visitante_raw)
                
                goles_totales = score_local + score_visitante
                
                url_summary = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/summary?event={event_id}"
                data = requests.get(url_summary, timeout=5).json()
                
                tiros_totales, corners_totales = 0, 0
                for eq_stats in data.get('boxscore', {}).get('teams', []):
                    for stat in eq_stats.get('statistics', []):
                        if stat['name'] == 'totalShots': tiros_totales += int(stat['displayValue'])
                        elif stat['name'] == 'wonCorners': corners_totales += int(stat['displayValue'])
                
                perfil_local = obtener_perfil_equipo(liga_code, id_local)
                perfil_visitante = obtener_perfil_equipo(liga_code, id_visitante)
                
                franja_actual = min(minuto // 15, 5)
                minutos_restantes = 15 - (minuto % 15)
                prop_franja = minutos_restantes / 15.0
                
                goles_esperados = 0.0
                for i in range(franja_actual, 6):
                    factor_t = prop_franja if i == franja_actual else 1.0
                    goles_esperados += (perfil_local[i] + perfil_visitante[i]) * factor_t
                
                fraccion_jugada = minuto / 90.0
                factor_ritmo = 1.0
                if minuto > 10:
                    tiros_esp = 25 * fraccion_jugada
                    corners_esp = 10 * fraccion_jugada
                    factor_ritmo = ((tiros_totales / max(1, tiros_esp)) + (corners_totales / max(1, corners_esp))) / 2.0
                    factor_ritmo = max(0.5, min(2.0, factor_ritmo))
                    
                goles_esperados_ajustados = goles_esperados * factor_ritmo
                
                linea = 2.5
                goles_necesarios = int(np.floor(linea - goles_totales)) + 1
                
                if goles_necesarios <= 0:
                    prob_over = 100.0
                else:
                    prob_under = sum(poisson.pmf(i, goles_esperados_ajustados) for i in range(goles_necesarios))
                    prob_over = (1 - prob_under) * 100
                
                if prob_over > 60 or (100 - prob_over) > 60:
                    alertas_encontradas += 1
                    recomendacion = "OVER" if prob_over > 60 else "UNDER"
                    mensaje = (
                        f"🚨 *¡ALERTA EN VIVO!* 🚨\n"
                        f"⚽ *[{liga_nombre}]* {nombre}\n"
                        f"⏱️ Minuto: {minuto}'\n"
                        f"📊 Marcador: {score_local} - {score_visitante}\n"
                        f"📈 Stats: {tiros_totales} tiros | {corners_totales} córners\n"
                        f"⚙️ Ritmo: {factor_ritmo:.2f}x\n"
                        f"🎯 Over 2.5: {prob_over:.1f}% | Under: {100-prob_over:.1f}%\n"
                        f"🔥 *RECOMENDACIÓN: {recomendacion}*"
                    )
                    # Lo agregamos a la página web
                    resultados_html += f"<p style='color:red;'><b>{mensaje.replace(chr(10), '<br>')}</b></p><hr>"
                    # Y lo enviamos a Telegram
                    enviar_alerta_telegram(mensaje)
        except Exception:
            continue
            
    if partidos_vivos == 0:
        resultados_html += "<p>💤 No hay partidos en vivo en este momento.</p>"
    elif alertas_encontradas == 0:
        resultados_html += f"<p>✅ Se revisaron {partidos_vivos} partidos, pero no hay ventajas claras (Over/Under > 60%).</p>"
        
    return resultados_html

@app.route('/')
def home():
    # Cuando entras a la página, ejecuta el escaneo y muestra el resultado
    return escanear_partidos()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
