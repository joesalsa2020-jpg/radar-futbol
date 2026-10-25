import requests
import numpy as np
import os
import json
from scipy.stats import poisson
from datetime import datetime
from flask import Flask, redirect, render_template_string

# --- VARIABLES DE ENTORNO ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INFERSPORTS_API_KEY = os.environ.get("INFERSPORTS_API_KEY")

app = Flask(__name__)

SCANNER_ACTIVO = True
ALERTAS_ENVIADAS = set() 
MEMORIA_EQUIPOS = {}
HISTORIAL_FILE = "historial.json"
HISTORIAL = []

LIGAS_TOP = {
    "eng.1": "Premier League", "esp.1": "La Liga",
    "ita.1": "Serie A", "ger.1": "Bundesliga", "fra.1": "Ligue 1",
    "uefa.champions": "Champions League", "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League", "usa.1": "MLS", "mex.1": "Liga MX",
    "arg.1": "Liga Argentina", "bra.1": "Brasileirão", 
    "conmebol.libertadores": "Libertadores", "conmebol.sudamericana": "Sudamericana"
}

# --- MANEJO DE MEMORIA (JSON) ---
def cargar_historial():
    global HISTORIAL, ALERTAS_ENVIADAS
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, 'r', encoding='utf-8') as f:
            try:
                HISTORIAL = json.load(f)
                for h in HISTORIAL:
                    ALERTAS_ENVIADAS.add(h['id'])
            except:
                HISTORIAL = []

def guardar_historial():
    with open(HISTORIAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(HISTORIAL, f, ensure_ascii=False, indent=4)

cargar_historial()

# --- FUNCIONES BASE ---
def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def cuota_decimal_a_prob(cuota):
    if cuota <= 1.0: return 0.0
    return (1 / cuota) * 100

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

# --- API INFERSPORTS (Prioridad Pinnacle) ---
def buscar_cuota_infersports(equipo_local, equipo_visitante, periodo="full_time"):
    headers = {}
    if INFERSPORTS_API_KEY:
        headers = {"Authorization": f"Bearer {INFERSPORTS_API_KEY}"}

    try:
        r = requests.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=5)
        if r.status_code != 200: return None
        eventos = r.json().get('data', [])
        
        event_id = None
        for ev in eventos:
            h_team = ev.get('home_team', {}).get('name', '').lower()
            a_team = ev.get('away_team', {}).get('name', '').lower()
            
            h_palabra = equipo_local.split()[0].lower()
            a_palabra = equipo_visitante.split()[0].lower()
            
            if h_palabra in h_team and a_palabra in a_team:
                event_id = ev['id']
                break
                
        if not event_id: return None
            
        r_odds = requests.get(f"https://api.infersports.dev/v1/events/{event_id}/odds?format=decimal", headers=headers, timeout=5)
        if r_odds.status_code != 200: return None
        
        odds_list = r_odds.json().get('odds', [])
        
        mejor_cuota_pinnacle = None
        mejor_cuota_fallback = None

        for odd in odds_list:
            if odd.get('market_type') == 'totals' and odd.get('period') == periodo and odd.get('status') == 'open':
                linea = odd.get('line')
                precio_over = odd.get('prices', {}).get('over', 0)
                precio_under = odd.get('prices', {}).get('under', 0)
                
                if linea and precio_over > 1.0 and precio_under > 1.0:
                    datos_odd = {
                        "linea": linea, "cuota_over": precio_over, "cuota_under": precio_under,
                        "prob_casa_over": cuota_decimal_a_prob(precio_over), "prob_casa_under": cuota_decimal_a_prob(precio_under),
                        "bookmaker": odd.get('bookmaker', '')
                    }
                    
                    # Priorizar Pinnacle
                    if odd.get('bookmaker', '').lower() == 'pinnacle':
                        return datos_odd
                    # Guardar la primera que encuentre como respaldo
                    elif mejor_cuota_fallback is None:
                        mejor_cuota_fallback = datos_odd

        return mejor_cuota_fallback
    except Exception:
        return None

# --- VERIFICADOR DE RESULTADOS ---
def verificar_resultados():
    global HISTORIAL
    pendientes = [h for h in HISTORIAL if h['resultado'] == 'Pendiente']
    actualizado = False
    
    for h in pendientes:
        # Buscamos el partido en ESPN para ver si ya terminó
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{h['liga_code']}/summary?event={h['event_id']}"
        try:
            r = requests.get(url, timeout=5)
            data = r.json()
            
            # Si el partido está terminado
            if data.get('header', {}).get('competitions', [{}])[0].get('status', {}).get('type', {}).get('state') == 'post':
                comp = data['header']['competitions'][0]
                score_local = int(comp['competitors'][0].get('score', '0'))
                score_visitante = int(comp['competitors'][1].get('score', '0'))
                goles_totales = score_local + score_visitante
                
                goles_ht = 0
                if h['mercado'] == 'HT':
                    # Contamos goles antes del minuto 45
                    for ev in data.get('keyEvents', []):
                        if "Goal" in ev.get('type', {}).get('text', ''):
                            minuto = int(''.join(filter(str.isdigit, ev.get('clock', {}).get('displayValue', '0').split('+')[0])))
                            if minuto <= 45:
                                goles_ht += 1
                
                # Evaluamos si ganó o perdió
                if h['mercado'] == 'HT':
                    if h['recomendacion'] == 'OVER':
                        h['resultado'] = 'Ganada' if goles_ht > h['linea'] else 'Perdida'
                    else:
                        h['resultado'] = 'Ganada' if goles_ht < h['linea'] else 'Perdida'
                else: # FT
                    if h['recomendacion'] == 'OVER':
                        h['resultado'] = 'Ganada' if goles_totales > h['linea'] else 'Perdida'
                    else:
                        h['resultado'] = 'Ganada' if goles_totales < h['linea'] else 'Perdida'
                
                actualizado = True
        except Exception:
            continue
            
    if actualizado:
        guardar_historial()

# --- ESCÁNER PRINCIPAL ---
def escanear_partidos():
    global SCANNER_ACTIVO
    if not SCANNER_ACTIVO: return
    
    verificar_resultados() # Verificar apuestas anteriores primero

    for liga_code, liga_nombre in LIGAS_TOP.items():
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/scoreboard"
        try:
            r = requests.get(url, timeout=5)
            eventos = r.json().get('events', [])
            for partido in eventos:
                if partido['status']['type']['state'] != 'in': continue
                
                event_id = partido['id']
                nombre = partido['name']
                minuto_str = partido['status'].get('displayClock', '0')
                minuto = int(minuto_str.replace("'", "")) if "'" in minuto_str else int(minuto_str.split(":")[0]) if ":" in minuto_str else 45
                
                if minuto > 80: continue
                
                comp = partido['competitions'][0]
                equipo_local = comp['competitors'][0]['team']['displayName']
                equipo_visitante = comp['competitors'][1]['team']['displayName']
                
                score_local = int(comp['competitors'][0].get('score', {}).get('displayValue', 0) if isinstance(comp['competitors'][0].get('score'), dict) else comp['competitors'][0].get('score', 0))
                score_visitante = int(comp['competitors'][1].get('score', {}).get('displayValue', 0) if isinstance(comp['competitors'][1].get('score'), dict) else comp['competitors'][1].get('score', 0))
                goles_totales = score_local + score_visitante
                
                url_summary = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/summary?event={event_id}"
                data = requests.get(url_summary, timeout=5).json()
                tiros, corners = 0, 0
                for eq_stats in data.get('boxscore', {}).get('teams', []):
                    for stat in eq_stats.get('statistics', []):
                        if stat['name'] == 'totalShots': tiros += int(stat['displayValue'])
                        elif stat['name'] == 'wonCorners': corners += int(stat['displayValue'])
                
                fraccion = minuto / 90.0
                factor_ritmo = max(0.5, min(2.0, ((tiros / max(1, 25 * fraccion)) + (corners / max(1, 10 * fraccion))) / 2.0)) if minuto > 10 else 1.0

                # --- LÓGICA HT ---
                if 15 <= minuto <= 35 and goles_totales == 0 and factor_ritmo > 1.1:
                    perfil_local = obtener_perfil_equipo(liga_code, comp['competitors'][0]['team']['id'])
                    perfil_visitante = obtener_perfil_equipo(liga_code, comp['competitors'][1]['team']['id'])
                    prob_ht = sum(perfil_local[0:3]) + sum(perfil_visitante[0:3])
                    
                    if prob_ht > 0.9:
                        clave = f"{event_id}_HT"
                        if clave not in ALERTAS_ENVIADAS:
                            ALERTAS_ENVIADAS.add(clave)
                            cuotas_ht = buscar_cuota_infersports(equipo_local, equipo_visitante, "half_time")
                            
                            if cuotas_ht:
                                linea = cuotas_ht['linea']
                                msj = f"🔥 *¡VALOR ENCONTRADO HT!* 🔥\n⚽ *[{liga_nombre}]* {nombre}\n⏱️ Minuto: {minuto}'\n📊 Marcador: {score_local} - {score_visitante}\n📈 Stats: {tiros} tiros | {corners} córners\n🎯 Línea HT: {linea}\n💰 Cuota OVER: {cuotas_ht['cuota_over']:.2f} ({cuotas_ht['bookmaker']})\n🔥 *RECOMENDACIÓN: OVER HT*"
                            else:
                                linea = 0.5 # Línea por defecto si no hay cuota
                                msj = f"🔥 *¡ALERTA DE GOL HT!* 🔥\n⚽ *[{liga_nombre}]* {nombre}\n⏱️ Minuto: {minuto}'\n📊 Marcador: {score_local} - {score_visitante}\n📈 Stats: {tiros} tiros | {corners} córners\n⚠️ _Cuota HT no disponible._\n🔥 *RECOMENDACIÓN: OVER HT*"
                            
                            enviar_alerta_telegram(msj)
                            
                            # Guardar en historial
                            HISTORIAL.append({
                                "id": clave, "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "liga_code": liga_code, "event_id": event_id, "partido": nombre,
                                "mercado": "HT", "recomendacion": "OVER", "linea": linea,
                                "cuota": cuotas_ht['cuota_over'] if cuotas_ht else 0,
                                "resultado": "Pendiente"
                            })
                            guardar_historial()

                # --- LÓGICA FT ---
                elif 55 <= minuto <= 80:
                    clave_ft = f"{event_id}_FT"
                    if clave_ft not in ALERTAS_ENVIADAS:
                        cuotas_ft = buscar_cuota_infersports(equipo_local, equipo_visitante, "full_time")
                        
                        if cuotas_ft:
                            linea = cuotas_ft['linea']
                            perfil_local = obtener_perfil_equipo(liga_code, comp['competitors'][0]['team']['id'])
                            perfil_visitante = obtener_perfil_equipo(liga_code, comp['competitors'][1]['team']['id'])
                            
                            franja = min(minuto // 15, 5)
                            prop_franja = (15 - (minuto % 15)) / 15.0
                            goles_esp = sum((perfil_local[i] + perfil_visitante[i]) * (prop_franja if i == franja else 1.0) for i in range(franja, 6))
                            goles_ajustados = goles_esp * factor_ritmo
                            
                            goles_nec = int(np.floor(linea - goles_totales)) + 1
                            prob_over = 100.0 if goles_nec <= 0 else (1 - sum(poisson.pmf(i, goles_ajustados) for i in range(goles_nec))) * 100
                            prob_under = 100.0 - prob_over
                            
                            valor_over = prob_over - cuotas_ft['prob_casa_over']
                            valor_under = prob_under - cuotas_ft['prob_casa_under']
                            
                            if valor_over > 5 or valor_under > 5:
                                ALERTAS_ENVIADAS.add(clave_ft)
                                rec = "OVER" if valor_over > valor_under else "UNDER"
                                prob_casa = cuotas_ft['prob_casa_over'] if rec == "OVER" else cuotas_ft['prob_casa_under']
                                prob_bot = prob_over if rec == "OVER" else prob_under
                                cuota_final = cuotas_ft['cuota_over'] if rec == "OVER" else cuotas_ft['cuota_under']
                                
                                msj = f"🚨 *¡VALOR EN FULL-TIME!* 🚨\n⚽ *[{liga_nombre}]* {nombre}\n⏱️ Minuto: {minuto}'\n📊 Marcador: {score_local} - {score_visitante}\n📈 Stats: {tiros} tiros | {corners} córners\n🎯 Línea: {linea}\n💰 Cuota {rec}: {cuota_final:.2f} ({cuotas_ft['bookmaker']})\n📊 Casa: {prob_casa:.1f}% vs Bot: {prob_bot:.1f}%\n🔥 *RECOMENDACIÓN: {rec}*"
                                enviar_alerta_telegram(msj)
                                
                                HISTORIAL.append({
                                    "id": clave_ft, "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "liga_code": liga_code, "event_id": event_id, "partido": nombre,
                                    "mercado": "FT", "recomendacion": rec, "linea": linea,
                                    "cuota": cuota_final, "resultado": "Pendiente"
                                })
                                guardar_historial()

        except Exception:
            continue

# --- DISEÑO DEL DASHBOARD (HTML) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Dashboard de Apuestas</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; background-color: #121212; color: white; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .btn { padding: 10px 20px; border: none; border-radius: 5px; font-size: 16px; cursor: pointer; text-decoration: none; color: white; }
        .btn-stop { background-color: #ff4d4d; }
        .btn-start { background-color: #4CAF50; }
        .stats-box { background-color: #1e1e1e; padding: 20px; border-radius: 10px; margin-bottom: 20px; display: flex; justify-content: space-around; }
        .stat-item { text-align: center; }
        .stat-value { font-size: 24px; font-weight: bold; margin-top: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; background-color: #1e1e1e; border-radius: 10px; overflow: hidden; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #333; }
        th { background-color: #333; }
        .ganada { color: #4CAF50; font-weight: bold; }
        .perdida { color: #ff4d4d; font-weight: bold; }
        .pendiente { color: #FFC107; font-weight: bold; }
    </style>
    <meta http-equiv="refresh" content="60">
</head>
<body>
    <div class="header">
        <h1>Panel de Control</h1>
        {% if scanner_activo %}
            <a href="/stop" class="btn btn-stop">🛑 Detener Escáner</a>
        {% else %}
            <a href="/start" class="btn btn-start">▶ Reanudar Escáner</a>
        {% endif %}
    </div>

    <div class="stats-box">
        <div class="stat-item"><div>Total Apuestas</div><div class="stat-value">{{ total }}</div></div>
        <div class="stat-item"><div>Ganadas</div><div class="stat-value ganada">{{ ganadas }}</div></div>
        <div class="stat-item"><div>Perdidas</div><div class="stat-value perdida">{{ perdidas }}</div></div>
        <div class="stat-item"><div>Win Rate</div><div class="stat-value">{{ winrate }}%</div></div>
    </div>

    <table>
        <tr>
            <th>Fecha</th>
            <th>Partido</th>
            <th>Mercado</th>
            <th>Rec.</th>
            <th>Línea</th>
            <th>Cuota</th>
            <th>Resultado</th>
        </tr>
        {% for apuesta in historial %}
        <tr>
            <td>{{ apuesta.fecha }}</td>
            <td>{{ apuesta.partido }}</td>
            <td>{{ apuesta.mercado }}</td>
            <td>{{ apuesta.recomendacion }}</td>
            <td>{{ apuesta.linea }}</td>
            <td>{{ apuesta.cuota }}</td>
            <td class="{{ apuesta.resultado|lower }}">{{ apuesta.resultado }}</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

# --- RUTAS FLASK ---
@app.route('/')
def home():
    if SCANNER_ACTIVO:
        escanear_partidos() # Escanea al entrar a la página
    
    # Calcular estadísticas
    total = len(HISTORIAL)
    ganadas = sum(1 for h in HISTORIAL if h['resultado'] == 'Ganada')
    perdidas = sum(1 for h in HISTORIAL if h['resultado'] == 'Perdida')
    evaluadas = ganadas + perdidas
    winrate = round((ganadas / evaluadas * 100), 1) if evaluadas > 0 else 0.0
    
    return render_template_string(
        HTML_TEMPLATE, 
        scanner_activo=SCANNER_ACTIVO,
        historial=reversed(HISTORIAL), # Mostrar las más recientes primero
        total=total, ganadas=ganadas, perdidas=perdidas, winrate=winrate
    )

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
