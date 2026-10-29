import requests, numpy as np, os, json, threading, time
from scipy.stats import poisson
from datetime import datetime
from flask import Flask, redirect, render_template_string

# ==== VARIABLES DE ENTORNO ====
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID")
INFERSPORTS_API_KEY = os.environ.get("INFERSPORTS_API_KEY")

app = Flask(__name__)

# ==== ESTADO GLOBAL ====
SCANNER_ACTIVO   = True
ALERTAS_ENVIADAS = set()
MEMORIA_EQUIPOS  = {}
HISTORIAL        = []
ESTADO_INFO      = {
    "ultimo_scan": "-",
    "vivos": 0,
    "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "ciclо": 0,
}

HISTORIAL_FILE = "historial.json"

LIGAS_TOP = {
    "eng.1": "Premier League", "eng.3": "League One", "esp.1": "La Liga",
    "ita.1": "Serie A", "ger.1": "Bundesliga", "fra.1": "Ligue 1",
    "uefa.champions": "Champions League", "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League", "usa.1": "MLS", "mex.1": "Liga MX",
    "arg.1": "Liga Argentina", "bra.1": "Brasileirão",
    "conmebol.libertadores": "Libertadores", "conmebol.sudamericana": "Sudamericana"
}

UMBRAL_HT_PROB = 42.0
UMBRAL_FT_VALOR = 5.0
PERFIL_DEFAULT = {
    "profile": [0.15, 0.16, 0.18, 0.18, 0.17, 0.16],
    "gf_avg": 1.3, "ga_avg": 1.3,
    "timestamp": 0, "valido": False,
}

# ==== HISTORIAL ====
def cargar_historial():
    global HISTORIAL, ALERTAS_ENVIADAS
    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, 'r', encoding='utf-8') as f:
                HISTORIAL = json.load(f)
            for h in HISTORIAL:
                ALERTAS_ENVIADAS.add(h['id'])
        except Exception:
            HISTORIAL = []

def guardar_historial():
    try:
        with open(HISTORIAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(HISTORIAL, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

cargar_historial()

# ==== TELEGRAM (solo envía) ====
def enviar_alerta_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception:
        pass

# ==== UTILIDADES ====
def cuota_decimal_a_prob(cuota):
    return (1 / cuota) * 100 if cuota > 1.0 else 0.0

def _parse_score(val):
    if isinstance(val, dict):
        return int(val.get('value', val.get('displayValue', 0)) or 0)
    try: return int(val or 0)
    except: return 0

# ==== PERFIL DE EQUIPO ====
def obtener_perfil_equipo(liga, team_id):
    ahora = time.time()
    if team_id in MEMORIA_EQUIPOS:
        entry = MEMORIA_EQUIPOS[team_id]
        if ahora - entry['timestamp'] < 2 * 86400 and entry.get('valido'):
            return entry

    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/teams/{team_id}/schedule"
    distribucion = [0.0] * 6
    total_goles, goles_for, goles_against, partidos = 0, 0, 0, 0

    try:
        eventos = requests.get(url, timeout=6).json().get('events', [])
        completados = [e for e in eventos if e.get('competitions', [{}])[0]
                       .get('status', {}).get('type', {}).get('completed')]
        if len(completados) < 3:
            return PERFIL_DEFAULT

        for ev in completados[-6:]:
            comp = ev.get('competitions', [{}])[0]
            for t in comp.get('competitors', []):
                s = _parse_score(t.get('score', 0))
                if str(t.get('team', {}).get('id')) == str(team_id):
                    goles_for += s
                else:
                    goles_against += s
            partidos += 1

        for ev in completados[-5:]:
            try:
                u = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/summary?event={ev['id']}"
                data = requests.get(u, timeout=6).json()
                for k in data.get('keyEvents', []):
                    tipo = k.get('type', {}).get('text', '')
                    if ("Goal" in tipo or "Penalty" in tipo) and str(k.get('team', {}).get('id')) == str(team_id):
                        clock = k.get('clock', {}).get('displayValue', '0').split('+')[0]
                        try: m = int(''.join(filter(str.isdigit, clock)))
                        except: continue
                        distribucion[min(m // 15, 5)] += 1
                        total_goles += 1
            except Exception:
                continue
    except Exception:
        return PERFIL_DEFAULT

    if total_goles > 0:
        distribucion = [g / total_goles for g in distribucion]
    else:
        distribucion = PERFIL_DEFAULT["profile"]

    entry = {
        "profile": distribucion,
        "gf_avg": goles_for / partidos if partidos else 1.3,
        "ga_avg": goles_against / partidos if partidos else 1.3,
        "timestamp": ahora,
        "valido": partidos >= 3 and total_goles >= 3,
    }
    if entry["valido"]:
        MEMORIA_EQUIPOS[team_id] = entry
    return entry

# ==== CÁLCULO λ ====
def calcular_lambdas(pl, pv):
    return ((pl['gf_avg'] + pv['ga_avg']) / 2.0,
            (pv['gf_avg'] + pl['ga_avg']) / 2.0)

def calcular_lambda_remaining(pl, pv, lam_l, lam_v, minuto, tipo='HT'):
    if tipo == 'HT':
        franja_final, fin = 2, 46.5
    else:
        franja_final, fin = 5, 94.0
    if minuto >= fin: return 0.0
    fi = min(int(minuto // 15), franja_final)
    total = 0.0
    for f in range(fi, franja_final + 1):
        ini, ffin = f * 15, (f + 1) * 15
        if tipo == 'HT' and f == 2: ffin = fin
        elif tipo == 'FT' and f == 5: ffin = fin
        mins = (ffin - ini) if minuto <= ini else (ffin - minuto)
        if mins <= 0: continue
        fr = mins / 15.0
        total += lam_l * pl['profile'][f] * fr
        total += lam_v * pv['profile'][f] * fr
    return total

def prob_al_menos_uno(lam):
    return (1 - np.exp(-lam)) * 100 if lam > 0 else 0.0

# ==== CUOTAS ====
def buscar_cuota_infersports(eq_l, eq_v, periodo="full_time"):
    headers = {"Authorization": f"Bearer {INFERSPORTS_API_KEY}"} if INFERSPORTS_API_KEY else {}
    try:
        r = requests.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=6)
        if r.status_code != 200: return None
        eventos = r.json().get('data', [])
        ignorar = ["fc","sc","cd","tsg","de","at","the","club","cp","cf"]
        def limp(n): return [w.lower() for w in n.split() if w.lower() not in ignorar]
        lp, vp = limp(eq_l), limp(eq_v)
        event_id = None
        for ev in eventos:
            h = ev.get('home_team', {}).get('name', '').lower()
            a = ev.get('away_team', {}).get('name', '').lower()
            if any(p in h for p in lp) and any(p in a for p in vp):
                event_id = ev['id']; break
        if not event_id: return None
        r2 = requests.get(f"https://api.infersports.dev/v1/events/{event_id}/odds?format=decimal",
                          headers=headers, timeout=6)
        if r2.status_code != 200: return None
        mejor = None
        for odd in r2.json().get('odds', []):
            if odd.get('market_type') == 'totals' and odd.get('period') == periodo:
                lin = odd.get('line'); po = odd.get('prices', {}).get('over', 0); pu = odd.get('prices', {}).get('under', 0)
                if lin and po > 1.0 and pu > 1.0 and odd.get('status') != 'suspended':
                    d = {"linea": lin, "cuota_over": po, "cuota_under": pu,
                         "prob_casa_over": cuota_decimal_a_prob(po),
                         "prob_casa_under": cuota_decimal_a_prob(pu),
                         "bookmaker": odd.get('bookmaker', '')}
                    if odd.get('bookmaker', '').lower() == 'pinnacle': return d
                    elif mejor is None: mejor = d
        return mejor
    except Exception:
        return None

# ==== VERIFICAR RESULTADOS ====
def verificar_resultados():
    global HISTORIAL
    actualizado = False
    for h in HISTORIAL:
        if h.get('resultado') != 'Pendiente': continue
        try:
            u = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{h['liga_code']}/summary?event={h['event_id']}"
            data = requests.get(u, timeout=6).json()
            if data.get('header', {}).get('competitions', [{}])[0].get('status', {}).get('type', {}).get('state') != 'post':
                continue
            comp = data['header']['competitions'][0]
            sl = _parse_score(comp['competitors'][0].get('score', '0'))
            sv = _parse_score(comp['competitors'][1].get('score', '0'))
            gt = sl + sv
            ght = 0
            if h['mercado'] == 'HT':
                for ev in data.get('keyEvents', []):
                    if "Goal" in ev.get('type', {}).get('text', ''):
                        try:
                            m = int(''.join(filter(str.isdigit, ev.get('clock', {}).get('displayValue', '0').split('+')[0])))
                            if m <= 45: ght += 1
                        except: pass
                total_ref = ght
            else:
                total_ref = gt
            if h['recomendacion'] == 'OVER':
                h['resultado'] = 'Ganada' if total_ref > h['linea'] else 'Perdida'
            else:
                h['resultado'] = 'Ganada' if total_ref < h['linea'] else 'Perdida'
            actualizado = True
        except Exception:
            continue
    if actualizado:
        guardar_historial()

# ==== ESCANEO ====
def escanear_partidos():
    global HISTORIAL
    vivos = 0
    for liga_code, liga_nombre in LIGAS_TOP.items():
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/scoreboard"
        try:
            eventos = requests.get(url, timeout=6).json().get('events', [])
            for partido in eventos:
                if partido['status']['type']['state'] != 'in': continue
                vivos += 1
                event_id = partido['id']; nombre = partido['name']
                ms = partido['status'].get('displayClock', '0')
                minuto = int(ms.replace("'", "")) if "'" in ms else int(ms.split(":")[0]) if ":" in ms else 45
                if minuto > 80: continue
                comp = partido['competitions'][0]
                eq_l = comp['competitors'][0]['team']['displayName']
                eq_v = comp['competitors'][1]['team']['displayName']
                sl = _parse_score(comp['competitors'][0].get('score', 0))
                sv = _parse_score(comp['competitors'][1].get('score', 0))
                gt = sl + sv

                u = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga_code}/summary?event={event_id}"
                data = requests.get(u, timeout=6).json()
                tiros, corners = 0, 0
                for eq in data.get('boxscore', {}).get('teams', []):
                    for s in eq.get('statistics', []):
                        if s['name'] == 'totalShots': tiros += int(s['displayValue'])
                        elif s['name'] == 'wonCorners': corners += int(s['displayValue'])
                fr = minuto / 90.0
                factor = 1.0
                if minuto > 10:
                    factor = ((tiros / max(1, 25*fr)) + (corners / max(1, 10*fr))) / 2.0
                    factor = max(0.5, min(2.0, factor))

                # HT
                if 15 <= minuto <= 35 and gt == 0 and factor > 1.1:
                    clave = f"{event_id}_HT"
                    if clave not in ALERTAS_ENVIADAS:
                        pl = obtener_perfil_equipo(liga_code, comp['competitors'][0]['team']['id'])
                        pv = obtener_perfil_equipo(liga_code, comp['competitors'][1]['team']['id'])
                        ll, lv = calcular_lambdas(pl, pv)
                        lam = calcular_lambda_remaining(pl, pv, ll, lv, minuto, 'HT') * factor
                        prob = prob_al_menos_uno(lam)
                        if prob >= UMBRAL_HT_PROB:
                            ALERTAS_ENVIADAS.add(clave)
                            cuotas = buscar_cuota_infersports(eq_l, eq_v, "half_time")
                            if cuotas:
                                linea = cuotas['linea']
                                valor = prob - cuotas['prob_casa_over']
                                msj = (f"🔥 *VALOR HT* 🔥\n⚽ *[{liga_nombre}]* {nombre}\n"
                                       f"⏱️ {minuto}' | 0-0 | {tiros} tiros, {corners} córners (Ritmo {factor:.2f}x)\n"
                                       f"🔥 *RECOMENDACIÓN: OVER {linea} @ {cuotas['cuota_over']:.2f}* ({cuotas['bookmaker']})\n"
                                       f"📊 Bot: *{prob:.1f}%* vs Casa: {cuotas['prob_casa_over']:.1f}% → Edge {valor:+.1f}%")
                                cuota_reg = cuotas['cuota_over']
                            else:
                                linea = 0.5
                                msj = (f"🔥 *ALERTA GOL HT* 🔥\n⚽ *[{liga_nombre}]* {nombre}\n"
                                       f"⏱️ {minuto}' | 0-0 | {tiros} tiros, {corners} córners (Ritmo {factor:.2f}x)\n"
                                       f"🔥 *RECOMENDACIÓN: OVER 0.5 HT*\n"
                                       f"📊 Prob gol antes descanso: *{prob:.1f}%*\n⚠️ Sin cuota disponible.")
                                cuota_reg = 0
                            enviar_alerta_telegram(msj)
                            HISTORIAL.append({
                                "id": clave, "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "liga_code": liga_code, "event_id": event_id, "partido": nombre, "liga_nombre": liga_nombre,
                                "mercado": "HT", "recomendacion": "OVER", "linea": linea,
                                "cuota": cuota_reg, "resultado": "Pendiente"
                            })
                            guardar_historial()

                # FT (A PARTIR DEL MINUTO 70)
                elif 70 <= minuto <= 80:
                    clave = f"{event_id}_FT"
                    if clave not in ALERTAS_ENVIADAS:
                        cuotas = buscar_cuota_infersports(eq_l, eq_v, "full_time")
                        if cuotas:
                            pl = obtener_perfil_equipo(liga_code, comp['competitors'][0]['team']['id'])
                            pv = obtener_perfil_equipo(liga_code, comp['competitors'][1]['team']['id'])
                            ll, lv = calcular_lambdas(pl, pv)
                            lam = calcular_lambda_remaining(pl, pv, ll, lv, minuto, 'FT') * factor
                            linea = cuotas['linea']
                            nec = max(0, int(np.floor(linea - gt)) + 1)
                            prob_o = 100.0 if nec <= 0 else (1 - sum(poisson.pmf(i, lam) for i in range(nec))) * 100
                            prob_u = 100.0 - prob_o
                            v_o = prob_o - cuotas['prob_casa_over']
                            v_u = prob_u - cuotas['prob_casa_under']
                            if v_o > UMBRAL_FT_VALOR or v_u > UMBRAL_FT_VALOR:
                                ALERTAS_ENVIADAS.add(clave)
                                rec = "OVER" if v_o > v_u else "UNDER"
                                pb = prob_o if rec == "OVER" else prob_u
                                pc = cuotas['prob_casa_over'] if rec == "OVER" else cuotas['prob_casa_under']
                                cf = cuotas['cuota_over'] if rec == "OVER" else cuotas['cuota_under']
                                vv = v_o if rec == "OVER" else v_u
                                msj = (f"🚨 *VALOR FT* 🚨\n⚽ *[{liga_nombre}]* {nombre}\n"
                                       f"⏱️ {minuto}' | {sl}-{sv} | {tiros} tiros, {corners} córners (Ritmo {factor:.2f}x)\n"
                                       f"🔥 *RECOMENDACIÓN: {rec} {linea} @ {cf:.2f}* ({cuotas['bookmaker']})\n"
                                       f"📊 Bot: *{pb:.1f}%* vs Casa: {pc:.1f}% → Edge {vv:+.1f}%")
                                enviar_alerta_telegram(msj)
                                HISTORIAL.append({
                                    "id": clave, "fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "liga_code": liga_code, "event_id": event_id, "partido": nombre, "liga_nombre": liga_nombre,
                                    "mercado": "FT", "recomendacion": rec, "linea": linea,
                                    "cuota": cf, "resultado": "Pendiente"
                                })
                                guardar_historial()
        except Exception:
            continue
    return vivos

# ==== HILO DE FONDO ====
def loop_escaner():
    while True:
        try:
            if SCANNER_ACTIVO:
                verificar_resultados()
                vivos = escanear_partidos()
                ESTADO_INFO["vivos"] = vivos
                ESTADO_INFO["ultimo_scan"] = datetime.now().strftime("%H:%M:%S")
                ESTADO_INFO["ciclо"] += 1
                print(f"[{ESTADO_INFO['ultimo_scan']}] Vivos: {vivos} | Alertas: {len(HISTORIAL)} | Caché: {len(MEMORIA_EQUIPOS)}", flush=True)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Pausado", flush=True)
            if len(ALERTAS_ENVIADAS) > 500:
                ALERTAS_ENVIADAS.clear()
        except Exception as e:
            print(f"Loop err: {e}", flush=True)
        time.sleep(300)

# ==== DASHBOARD HTML (ESTILO NBA) ====
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Panel del Bot (Soccer)</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        :root {
            --bg: #0f172a;
            --card-bg: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --green: #10b981;
            --red: #ef4444;
            --yellow: #eab308;
            --blue: #3b82f6;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text-main); padding: 16px; max-width: 800px; margin: auto; }
        
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        h1 { margin: 0; font-size: 20px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
        .btn { padding: 10px 16px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; text-decoration: none; color: white; transition: 0.2s; }
        .btn-stop { background: var(--red); }
        .btn-start { background: var(--green); }
        
        .info-bar { display: flex; justify-content: space-between; background: var(--card-bg); padding: 12px 16px; border-radius: 12px; margin-bottom: 20px; font-size: 13px; color: var(--text-muted); }
        .info-bar span { font-weight: bold; color: var(--text-main); }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }
        .badge-on { background: rgba(16, 185, 129, 0.2); color: var(--green); }
        .badge-off { background: rgba(239, 68, 68, 0.2); color: var(--red); }

        .stats { background: var(--card-bg); padding: 20px; border-radius: 12px; margin-bottom: 24px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; text-align: center; }
        .stat-item { display: flex; flex-direction: column; gap: 4px; }
        .stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-value { font-size: 22px; font-weight: bold; }
        .v-green { color: var(--green); }
        .v-red { color: var(--red); }
        .v-yellow { color: var(--yellow); }

        .section-title { font-size: 13px; font-weight: bold; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
        
        .card-list { display: flex; flex-direction: column; gap: 12px; }
        .card { background: var(--card-bg); border-radius: 12px; padding: 16px; border-left: 4px solid var(--yellow); display: flex; flex-direction: column; gap: 8px; }
        .card.ganada { border-left-color: var(--green); }
        .card.perdida { border-left-color: var(--red); }
        
        .card-header { display: flex; justify-content: space-between; align-items: flex-start; }
        .card-title { font-size: 15px; font-weight: bold; margin: 0; }
        .card-liga { font-size: 12px; color: var(--text-muted); }
        
        .card-body { display: flex; justify-content: space-between; align-items: center; margin-top: 4px; }
        .bet-info { font-size: 14px; font-weight: bold; color: var(--blue); }
        .bet-cuota { font-size: 14px; color: var(--text-main); }
        
        .card-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 12px; }
        .edge { background: rgba(16, 185, 129, 0.15); color: var(--green); padding: 4px 8px; border-radius: 4px; font-weight: bold; }
        .time { color: var(--text-muted); }
        .res-badge { font-weight: bold; font-size: 11px; padding: 4px 8px; border-radius: 4px; }
        .res-pendiente { color: var(--yellow); }
        .res-ganada { color: var(--green); }
        .res-perdida { color: var(--red); }
    </style>
</head>
<body>
    <div class="header">
        <h1>⚽ Radar Fútbol</h1>
        {% if scanner_activo %}
            <a href="/stop" class="btn btn-stop">🛑 Detener</a>
        {% else %}
            <a href="/start" class="btn btn-start">▶ Reanudar</a>
        {% endif %}
    </div>

    <div class="info-bar">
        <div>Estado: <span class="badge {% if scanner_activo %}badge-on{% else %}badge-off{% endif %}">{% if scanner_activo %}ACTIVO{% else %}PAUSADO{% endif %}</span></div>
        <div>Último scan: <span>{{ ultimo_scan }}</span></div>
        <div>Vivos: <span>{{ vivos }}</span></div>
    </div>

    <div class="stats">
        <div class="stat-item">
            <div class="stat-label">Total</div>
            <div class="stat-value">{{ total }}</div>
        </div>
        <div class="stat-item">
            <div class="stat-label">Ganadas</div>
            <div class="stat-value v-green">{{ ganadas }}</div>
        </div>
        <div class="stat-item">
            <div class="stat-label">Perdidas</div>
            <div class="stat-value v-red">{{ perdidas }}</div>
        </div>
        <div class="stat-item">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value v-yellow">{{ winrate }}%</div>
        </div>
    </div>

    <div class="section-title">Señales Recientes</div>
    <div class="card-list">
        {% for a in historial %}
        <div class="card {{ a.resultado|lower }}">
            <div class="card-header">
                <div>
                    <p class="card-title">{{ a.partido }}</p>
                    <span class="card-liga">{{ a.liga_nombre }} | {{ a.mercado }}</span>
                </div>
                <span class="res-badge res-{{ a.resultado|lower }}">{{ a.resultado|upper }}</span>
            </div>
            
            <div class="card-body">
                <div class="bet-info">{{ a.recomendacion }} {{ a.linea }}</div>
                <div class="bet-cuota">@ {{ a.cuota }}</div>
            </div>
            
            <div class="card-footer">
                <div class="time">{{ a.fecha }}</div>
            </div>
        </div>
        {% else %}
        <div style="text-align: center; color: var(--text-muted); padding: 20px;">
            No hay señales todavía. El bot está escaneando...
        </div>
        {% endfor %}
    </div>
</body>
</html>
"""

# ==== RUTAS FLASK ====
@app.route('/')
def home():
    cargar_historial() # ¡Esto arregla que el panel se quede en 0!
    
    total = len(HISTORIAL)
    ganadas = sum(1 for h in HISTORIAL if h.get('resultado') == 'Ganada')
    perdidas = sum(1 for h in HISTORIAL if h.get('resultado') == 'Perdida')
    ev = ganadas + perdidas
    wr = round((ganadas / ev * 100), 1) if ev else 0.0
    
    return render_template_string(
        HTML_TEMPLATE,
        scanner_activo=SCANNER_ACTIVO,
        historial=list(reversed(HISTORIAL))[-50:], 
        total=total, ganadas=ganadas, perdidas=perdidas,
        winrate=wr, ultimo_scan=ESTADO_INFO["ultimo_scan"],
        vivos=ESTADO_INFO["vivos"]
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

@app.route('/ping')
def ping():
    return "pong", 200

# ==== ARRANQUE ====
def _start_scanner():
    t = threading.Thread(target=loop_escaner, daemon=True)
    t.start()
    print("🚀 Hilo del escáner iniciado", flush=True)

_start_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
