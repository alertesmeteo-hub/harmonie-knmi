import requests
import folium
from folium.features import DivIcon

# On utilise l'API v1 d'Opendatasoft (stable et increvable)
url = "https://public.opendatasoft.com/api/records/1.0/search/"
params = {
    "dataset": "donnees-synoptiques-courantes-temps-reel-synop",
    "rows": 1000,
    "sort": "-date"
}

print("Récupération des données Météo-France via l'API v1...")
try:
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    donnees = data.get('records', [])
except Exception as e:
    print(f"Erreur API: {e}")
    donnees = []

releves_pluie = {}

# Extraction des données
for obs in donnees:
    fields = obs.get('fields', {})
    sta = fields.get('numer_sta')
    rr24 = fields.get('rr24')
    coords = fields.get('coordonnees')
    date_obs = fields.get('date')
    
    if sta and coords and rr24 is not None:
        try:
            val = float(rr24)
            # On ne garde que les relevés avec de la pluie (>= 0.2 mm)
            if val >= 0.2:
                if sta not in releves_pluie or date_obs > releves_pluie[sta]['date']:
                    if isinstance(coords, list) and len(coords) == 2:
                        lat, lon = coords[0], coords[1]
                        releves_pluie[sta] = {
                            'val': val,
                            'lat': lat,
                            'lon': lon,
                            'date': date_obs
                        }
        except Exception:
            continue

# Création de la carte interactive
carte = folium.Map(location=[46.5, 2.5], zoom_start=6, tiles='OpenStreetMap')

for sta, data in releves_pluie.items():
    pluie = data['val']
    
    # Couleurs dynamiques selon le cumul
    if pluie < 5: couleur = 'blue'
    elif pluie < 15: couleur = '#008000'
    elif pluie < 40: couleur = '#ff8800'
    elif pluie < 70: couleur = 'red'
    else: couleur = 'purple'

    html_label = f'<div style="font-size: 11px; font-weight: bold; color: {couleur}; text-shadow: 1px 1px 0 #fff, -1px 1px 0 #fff, 1px -1px 0 #fff, -1px -1px 0 #fff;">{round(pluie, 1)}</div>'

    folium.Marker(
        location=[data['lat'], data['lon']],
        icon=DivIcon(
            icon_size=(30, 30),
            icon_anchor=(15, 15),
            html=html_label
        )
    ).add_to(carte)

carte.save('observations_pluie_24h.html')
print(f"Terminé ! {len(releves_pluie)} stations affichées sur la carte.")
